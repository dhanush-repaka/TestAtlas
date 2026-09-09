"""LLM gap-analysis layer: compares a repo's project documents, taken
together, against what the whole codebase actually contains, producing
structured findings.

Documents describe the system overall -- a process/architecture doc covering
multiple flows -- not one module each, so the comparison always runs against
the entire graph rather than a single business module's contents. (An
earlier version scoped this per-module; that added a linking step for no
real benefit, since a genuinely high-level doc usually touches several
modules at once anyway.)

Likewise, when a repo has more than one document, the comparison bundles
ALL of them into a single pass rather than analyzing each in isolation --
comparing one doc at a time can't see that another doc already covers what
looks like a gap, and can't tell "undocumented" from "documented elsewhere".
A repo's documents are one corpus being checked against one codebase.

Same non-live-API-call pattern as kg/enrichment.py: this module only builds
the comparison context -- the actual comparison is done by an agent (Claude
Code, or another LLM session) reading both sides and posting findings back
through the API, or by server/llm_gap_analysis.py's opt-in live call. There's
no mechanical way to compute "is this documented capability actually
implemented" -- it's a judgment call over natural language, same reasoning
as why enrichment's purpose summaries aren't AST-derived either.
"""
from __future__ import annotations

import networkx as nx

ALLOWED_GAP_CATEGORIES = {"missing_implementation", "undocumented_capability", "mismatch"}


def gap_analysis_context(g: nx.MultiDiGraph, docs: list[dict]) -> dict:
    """Everything an agent needs to compare a repo's documents -- combined,
    not one at a time -- against the codebase's real, complete contents:
    every module (business-domain grouping, see kg/dev_graph_builder.py),
    each with its purpose summary (if enriched) and the actual class/function
    names AST found in each of its files."""
    if not docs:
        return {"ok": False, "message": "This repo has no documents yet -- add at least one first."}

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
        "documents": [{"id": d["id"], "name": d["name"], "content": d["content"]} for d in docs],
        "modules": modules,
    }
