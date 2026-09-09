"""LLM gap-analysis layer: compares a project document's claims against what
its linked business module actually contains (kg/dev_graph_builder.py's
`domain` rollup), producing structured findings.

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
    """Everything an agent needs to compare `doc` against its linked module's
    real contents: every file in that domain, with its purpose summary (if
    enriched) and the actual class/function names AST found in it. Returns
    {"ok": False, "message": ...} instead of raising when the doc isn't
    linked yet or its domain doesn't exist in this run -- both are normal,
    actionable states, not errors."""
    domain = doc.get("domain")
    if not domain:
        return {
            "ok": False,
            "message": "This document isn't linked to a module yet -- set its `domain` field first "
            "(see the repo's Module scores for available names).",
        }

    file_nodes = [n for n, d in g.nodes(data=True) if d.get("type") == "File" and d.get("domain") == domain]
    if not file_nodes:
        available = sorted({d["domain"] for _, d in g.nodes(data=True) if d.get("type") == "File" and d.get("domain")})
        return {
            "ok": False,
            "message": f"No module named '{domain}' in this run. Available: {', '.join(available)}",
        }

    files = []
    for file_id in sorted(file_nodes):
        data = g.nodes[file_id]
        classes = [
            g.nodes[v]["label"] for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Class"
        ]
        functions = [
            g.nodes[v]["label"] for _, v, ed in g.out_edges(file_id, data=True)
            if ed.get("relation") == "DEFINES" and g.nodes[v].get("type") == "Function"
        ]
        files.append({
            "file": data["label"],
            "purpose": data.get("purpose"),
            "classes": sorted(classes),
            "functions": sorted(functions),
        })

    return {
        "ok": True,
        "document": {"id": doc["id"], "name": doc["name"], "content": doc["content"]},
        "module": {"name": domain, "files": files},
    }
