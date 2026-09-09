"""LLM gap-analysis layer: compares a project document's claims against what
the whole codebase actually contains, producing structured findings.

Documents describe the system overall -- a process/architecture doc covering
multiple flows -- not one module each, so the comparison always runs against
the entire graph rather than a single business module's contents. (An
earlier version scoped this per-module; that added a linking step for no
real benefit, since a genuinely high-level doc usually touches several
modules at once anyway.)

Same non-live-API-call pattern as kg/enrichment.py: this module only builds
the comparison context -- the actual comparison is done by an agent (Claude
Code, or another LLM session) reading both sides and posting findings back
through the API. There's no mechanical way to compute "is this documented
capability actually implemented" -- it's a judgment call over natural
language, same reasoning as why enrichment's purpose summaries aren't
AST-derived either.
"""
from __future__ import annotations

import networkx as nx

ALLOWED_GAP_CATEGORIES = {"missing_implementation", "undocumented_capability", "mismatch"}


def gap_analysis_context(g: nx.MultiDiGraph, doc: dict) -> dict:
    """Everything an agent needs to compare `doc` against the codebase's real,
    complete contents: every module (business-domain grouping, see
    kg/dev_graph_builder.py), each with its purpose summary (if enriched) and
    the actual class/function names AST found in each of its files."""
    file_nodes = [n for n, d in g.nodes(data=True) if d.get("type") == "File"]
    if not file_nodes:
        return {"ok": False, "message": "This run's graph has no files to compare against."}

    by_domain: dict[str, list[dict]] = {}
    for file_id in sorted(file_nodes):
        data = g.nodes[file_id]
        domain = data.get("domain") or "(ungrouped)"
        classes = [
            g.nodes[v]["label"] for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Class"
        ]
        functions = [
            g.nodes[v]["label"] for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Function"
        ]
        by_domain.setdefault(domain, []).append({
            "file": data["label"],
            "purpose": data.get("purpose"),
            "classes": sorted(classes),
            "functions": sorted(functions),
        })

    modules = [{"module": domain, "files": files} for domain, files in sorted(by_domain.items())]

    return {
        "ok": True,
        "document": {"id": doc["id"], "name": doc["name"], "content": doc["content"]},
        "modules": modules,
    }
