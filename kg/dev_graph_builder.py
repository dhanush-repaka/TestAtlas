"""Builds a networkx.MultiDiGraph knowledge graph from a ParsedPythonRepo
(dev/application code, not test code) and rolls it up into business-module
scores.

Schema:
  File     node  -- one per .py file (what the old schema called "Module" --
                    renamed because "Module" now means something more useful:
                    see below)
  Class    node
  Function node  -- functions AND methods (methods carry class=<qualname>)

  IMPORTS  File   -> File        (in-repo import of another file)
  DEFINES  File   -> Class       (class declared at module level)
  DEFINES  File   -> Function    (top-level function)
  DEFINES  Class  -> Function    (method)
  CALLS    Function -> Function  (best-effort resolved call)

Everything above is purely mechanical (AST-derived) -- no purpose/summary
fields are set here. A later, separate LLM enrichment pass adds `purpose`
onto these same nodes without touching the structure (kg/enrichment.py).

"Module" here means what most people actually mean by it in conversation --
a business capability (Accounts, Payments, Statements), not a single .py
file. Every File/Class/Function gets a `domain` attribute: by default the
file's immediate containing package (free, mechanical, and correct for most
feature-organized codebases -- `app.accounts.models` and `app.accounts.views`
both land in domain `app.accounts`). kg/enrichment.py lets that default be
overridden per file where folder structure doesn't reflect real domains
(layered architectures: controllers/, services/, models/ split across a
feature). `score_modules()` rolls per-file centrality up to this domain
grouping -- that's what actually renders in the UI's "Module scores" panel.
"""
from __future__ import annotations

import networkx as nx

from .python_ast_parser import ParsedPythonRepo


def _default_domain(dotted: str) -> str:
    """Immediate containing package of a dotted file name -- the free,
    zero-LLM default for which business module a file belongs to. A file with
    no package (top-level script) is its own singleton domain."""
    parts = dotted.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else dotted


def build_dev_graph(repo: ParsedPythonRepo) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    for dotted, module in repo.modules.items():
        file_id = f"file:{dotted}"
        g.add_node(file_id, type="File", label=dotted, path=module.path, domain=_default_domain(dotted))

    for dotted, module in repo.modules.items():
        file_id = f"file:{dotted}"
        domain = g.nodes[file_id]["domain"]
        for target in module.imports:
            target_id = f"file:{target}"
            if g.has_node(target_id):
                g.add_edge(file_id, target_id, relation="IMPORTS")

        for cls in module.classes:
            cls_id = f"class:{cls.qualname}"
            g.add_node(cls_id, type="Class", label=cls.name, defined_in=dotted, domain=domain, bases=cls.bases, path=module.path)
            g.add_edge(file_id, cls_id, relation="DEFINES")

        for fn in module.functions:
            fn_id = f"function:{fn.qualname}"
            g.add_node(
                fn_id, type="Function", label=fn.name, defined_in=dotted, domain=domain,
                is_method=fn.is_method, path=module.path,
            )
            if fn.is_method and fn.class_qualname:
                g.add_edge(f"class:{fn.class_qualname}", fn_id, relation="DEFINES")
            else:
                g.add_edge(file_id, fn_id, relation="DEFINES")

    for module in repo.modules.values():
        for fn in module.functions:
            fn_id = f"function:{fn.qualname}"
            for called in fn.calls:
                target_id = f"function:{called}"
                if g.has_node(target_id) and target_id != fn_id:
                    g.add_edge(fn_id, target_id, relation="CALLS")

    g.graph["skipped_files"] = repo.skipped_files
    return g


def _file_centrality(g: nx.MultiDiGraph, file_nodes: list[str]) -> tuple[dict, dict, nx.MultiDiGraph]:
    """PageRank + betweenness over a File-level graph: IMPORTS edges directly,
    plus CALLS edges folded up to their owning files (so a file whose
    functions are heavily called shows up as central even without a surviving
    direct import edge)."""
    file_graph = nx.MultiDiGraph()
    file_graph.add_nodes_from(file_nodes)
    for u, v, d in g.edges(data=True):
        if d.get("relation") == "IMPORTS" and g.nodes[u].get("type") == "File" and g.nodes[v].get("type") == "File":
            file_graph.add_edge(u, v)

    owner_file: dict[str, str] = {}
    for n, d in g.nodes(data=True):
        if d.get("type") in ("Function", "Class") and d.get("defined_in"):
            owner_file[n] = f"file:{d['defined_in']}"

    for u, v, d in g.edges(data=True):
        if d.get("relation") != "CALLS":
            continue
        fu, fv = owner_file.get(u), owner_file.get(v)
        if fu and fv and fu != fv:
            file_graph.add_edge(fu, fv)

    try:
        pagerank = nx.pagerank(file_graph) if file_graph.number_of_edges() else {n: 0.0 for n in file_nodes}
    except Exception:  # noqa: BLE001 -- numpy/scipy missing, or degenerate graph
        pagerank = {n: 0.0 for n in file_nodes}
    try:
        betweenness = (
            nx.betweenness_centrality(nx.DiGraph(file_graph)) if file_graph.number_of_edges() else {n: 0.0 for n in file_nodes}
        )
    except Exception:  # noqa: BLE001
        betweenness = {n: 0.0 for n in file_nodes}
    return pagerank, betweenness, file_graph


def score_modules(g: nx.MultiDiGraph) -> list[dict]:
    """Business-module (domain) scores: per-file PageRank/betweenness, summed
    up by each file's `domain` (the mechanical default, or an LLM-assigned
    override from kg/enrichment.py). Honest about coverage: a domain with no
    cross-domain import edges at all is flagged `isolated=True` rather than
    silently reported as "unimportant" -- that distinction matters, per the
    psf/requests proof-of-concept, where coverage gaps can otherwise look
    identical to genuinely low-value code.
    """
    file_nodes = [n for n, d in g.nodes(data=True) if d.get("type") == "File"]
    if not file_nodes:
        return []

    pagerank, betweenness, file_graph = _file_centrality(g, file_nodes)

    # Domain-level import graph: collapse File-level IMPORTS up to their
    # owning domains, keeping only edges that actually cross a domain
    # boundary (an import within the same domain isn't a domain dependency).
    domain_of = {n: g.nodes[n]["domain"] for n in file_nodes}
    domain_graph = nx.MultiDiGraph()
    domain_graph.add_nodes_from(set(domain_of.values()))
    for u, v in file_graph.edges():
        du, dv = domain_of[u], domain_of[v]
        if du != dv:
            domain_graph.add_edge(du, dv)

    domains: dict[str, dict] = {}
    for file_id in file_nodes:
        data = g.nodes[file_id]
        domain = data["domain"]
        bucket = domains.setdefault(domain, {
            "module": domain, "files": [], "pagerank": 0.0, "betweenness": 0.0,
            "class_count": 0, "function_count": 0, "purposes": [],
        })
        bucket["files"].append(data["label"])
        bucket["pagerank"] += pagerank.get(file_id, 0.0)
        bucket["betweenness"] += betweenness.get(file_id, 0.0)
        bucket["class_count"] += sum(
            1 for _, v, d2 in g.out_edges(file_id, data=True) if d2.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Class"
        )
        bucket["function_count"] += sum(
            1 for _, v, d2 in g.out_edges(file_id, data=True) if d2.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Function"
        )
        if data.get("purpose"):
            bucket["purposes"].append(data["purpose"])

    results = []
    for domain, bucket in domains.items():
        in_deg = domain_graph.in_degree(domain) if domain in domain_graph else 0
        out_deg = domain_graph.out_degree(domain) if domain in domain_graph else 0
        results.append({
            "module": domain,
            "files": sorted(bucket["files"]),
            "file_count": len(bucket["files"]),
            "pagerank": round(bucket["pagerank"], 6),
            "betweenness": round(bucket["betweenness"], 6),
            "imported_by_count": in_deg,
            "imports_count": out_deg,
            "class_count": bucket["class_count"],
            "function_count": bucket["function_count"],
            "isolated": in_deg == 0 and out_deg == 0,
            # A domain purpose isn't its own LLM enrichment target -- it's
            # just its member files' purposes strung together, so this shows
            # up as soon as any file in the domain has been enriched, no
            # separate pass needed.
            "purpose": " ".join(bucket["purposes"]) if bucket["purposes"] else None,
        })

    results.sort(key=lambda r: (r["pagerank"], r["betweenness"]), reverse=True)
    return results
