"""Builds a networkx.MultiDiGraph knowledge graph from a ParsedPythonRepo
(dev/application code, not test code) and rolls it up into module-wise scores.

Schema (parallels kg/graph_builder.py's test-code schema):
  Module   node  -- one per .py file
  Class    node
  Function node  -- functions AND methods (methods carry class=<qualname>)

  IMPORTS  Module  -> Module     (module-level import of another module in-repo)
  DEFINES  Module  -> Class      (class declared at module level)
  DEFINES  Module  -> Function   (top-level function)
  DEFINES  Class   -> Function   (method)
  CALLS    Function -> Function  (best-effort resolved call)

Everything here is purely mechanical (AST-derived) -- no purpose/summary
fields are set. A later, separate LLM enrichment pass is expected to add
`purpose` attributes onto these same nodes without touching the structure.
"""
from __future__ import annotations

import networkx as nx

from .python_ast_parser import ParsedPythonRepo


def build_dev_graph(repo: ParsedPythonRepo) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    for dotted, module in repo.modules.items():
        mod_id = f"module:{dotted}"
        g.add_node(mod_id, type="Module", label=dotted, file=module.path)

    for dotted, module in repo.modules.items():
        mod_id = f"module:{dotted}"
        for target in module.imports:
            target_id = f"module:{target}"
            if g.has_node(target_id):
                g.add_edge(mod_id, target_id, relation="IMPORTS")

        for cls in module.classes:
            cls_id = f"class:{cls.qualname}"
            g.add_node(cls_id, type="Class", label=cls.name, module=dotted, bases=cls.bases, file=module.path)
            g.add_edge(mod_id, cls_id, relation="DEFINES")

        for fn in module.functions:
            fn_id = f"function:{fn.qualname}"
            g.add_node(
                fn_id, type="Function", label=fn.name, module=dotted,
                is_method=fn.is_method, file=module.path,
            )
            if fn.is_method and fn.class_qualname:
                g.add_edge(f"class:{fn.class_qualname}", fn_id, relation="DEFINES")
            else:
                g.add_edge(mod_id, fn_id, relation="DEFINES")

    for module in repo.modules.values():
        for fn in module.functions:
            fn_id = f"function:{fn.qualname}"
            for called in fn.calls:
                target_id = f"function:{called}"
                if g.has_node(target_id) and target_id != fn_id:
                    g.add_edge(fn_id, target_id, relation="CALLS")

    g.graph["skipped_files"] = repo.skipped_files
    return g


def score_modules(g: nx.MultiDiGraph) -> list[dict]:
    """Rolls per-node PageRank + betweenness centrality up into per-module
    scores. Honest about coverage: a module with no in/out edges at all
    (never imported, imports nothing, defines nothing that's called) is
    flagged `isolated=True` rather than silently reported as "unimportant" --
    that distinction matters, per the psf/requests proof-of-concept, where
    coverage gaps can otherwise look identical to genuinely low-value code.
    """
    module_nodes = [n for n, d in g.nodes(data=True) if d.get("type") == "Module"]
    if not module_nodes:
        return []

    # Module-level graph: collapse IMPORTS edges between modules directly,
    # and also let each module inherit call-centrality from its own functions/classes.
    module_graph = nx.MultiDiGraph()
    module_graph.add_nodes_from(module_nodes)
    for u, v, d in g.edges(data=True):
        if d.get("relation") != "IMPORTS":
            continue
        if g.nodes[u].get("type") == "Module" and g.nodes[v].get("type") == "Module":
            module_graph.add_edge(u, v)

    # Fold function-level CALLS into module-to-module edges too, so a module
    # whose functions are heavily called (even without a direct import edge
    # surviving simplification) still shows up as central.
    owner_module: dict[str, str] = {}
    for n, d in g.nodes(data=True):
        if d.get("type") in ("Function", "Class") and d.get("module"):
            owner_module[n] = f"module:{d['module']}"

    for u, v, d in g.edges(data=True):
        if d.get("relation") != "CALLS":
            continue
        mu, mv = owner_module.get(u), owner_module.get(v)
        if mu and mv and mu != mv:
            module_graph.add_edge(mu, mv)

    try:
        pagerank = nx.pagerank(module_graph) if module_graph.number_of_edges() else {n: 0.0 for n in module_nodes}
    except Exception:  # noqa: BLE001 -- numpy/scipy missing, or degenerate graph
        pagerank = {n: 0.0 for n in module_nodes}

    try:
        betweenness = (
            nx.betweenness_centrality(nx.DiGraph(module_graph))
            if module_graph.number_of_edges()
            else {n: 0.0 for n in module_nodes}
        )
    except Exception:  # noqa: BLE001
        betweenness = {n: 0.0 for n in module_nodes}

    results = []
    for mod_id in module_nodes:
        data = g.nodes[mod_id]
        in_deg = module_graph.in_degree(mod_id)
        out_deg = module_graph.out_degree(mod_id)
        n_classes = sum(1 for _, v, d2 in g.out_edges(mod_id, data=True) if d2.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Class")
        n_functions = sum(1 for _, v, d2 in g.out_edges(mod_id, data=True) if d2.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Function")
        results.append({
            "module": data["label"],
            "file": data["file"],
            "pagerank": round(pagerank.get(mod_id, 0.0), 6),
            "betweenness": round(betweenness.get(mod_id, 0.0), 6),
            "imported_by_count": in_deg,
            "imports_count": out_deg,
            "class_count": n_classes,
            "function_count": n_functions,
            "isolated": in_deg == 0 and out_deg == 0,
            # Populated once kg/enrichment.py has annotated this module -- None
            # until then, distinguishable from "" so the UI can tell "not yet
            # enriched" apart from "an LLM pass ran and had nothing to say".
            "purpose": data.get("purpose"),
        })

    results.sort(key=lambda r: (r["pagerank"], r["betweenness"]), reverse=True)
    return results
