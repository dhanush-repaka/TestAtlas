"""LLM semantic-enrichment layer, applied on top of the mechanical dev-code
graph (kg/dev_graph_builder.py, kg/python_ast_parser.py).

Deliberately NOT a live API call from the always-running backend -- per the
project's own cost constraint, the "LLM" here is Claude Code itself (this
session, or a subagent it spawns), reading the actual source files and
writing purpose summaries. That makes this a two-step, human-in-the-loop-free
but backend-cost-free pipeline:

  1. `enrichment_targets(g)` -- ask the graph what still needs a summary
     (file path + id for every File/Class lacking one). An agent reads
     those files and writes back
     {node_id: {"purpose": "...", "tags": [...], "domain": "..."}}
  2. `apply_enrichment(g, entries)` -- merge that dict onto the graph's node
     attributes in place. Purely additive: no structural node/edge changes,
     so re-running the mechanical analysis later won't conflict with it,
     though it WILL wipe purposes for a repo, since it's a fresh graph --
     enrichment is stored per-run, not "sticky" across runs (see server/app.py).

Scoped to File and Class nodes, not every Function -- a 20-file repo already
has 100+ functions, more than a single review pass usefully summarizes,
whereas file/class purpose is exactly the "what does this piece of the
system do" signal the module-score feature is missing.

`domain` is the one File-only field: it overrides kg/dev_graph_builder.py's
free default (a file's immediate containing package) with a business-domain
label an agent judges to be more accurate -- e.g. a layered architecture
where `controllers/accounts_controller.py`, `services/accounts_service.py`,
and `models/account.py` should all roll up under "Accounts" despite living
in different folders. `score_modules()` re-groups by this field automatically
the next time it runs, no separate plumbing needed.
"""
from __future__ import annotations

import networkx as nx

_ENRICHABLE_TYPES = ("File", "Class")


def enrichment_targets(g: nx.MultiDiGraph) -> list[dict]:
    """Nodes worth a purpose summary that don't have one yet."""
    targets = []
    for n, data in g.nodes(data=True):
        if data.get("type") not in _ENRICHABLE_TYPES:
            continue
        if data.get("purpose"):
            continue
        targets.append({
            "node_id": n,
            "type": data["type"],
            "label": data.get("label", n),
            "path": data.get("path"),
            "domain": data.get("domain"),
        })
    targets.sort(key=lambda t: (t["path"] or "", t["type"], t["label"]))
    return targets


def enrichment_coverage(g: nx.MultiDiGraph) -> dict:
    total = sum(1 for _, d in g.nodes(data=True) if d.get("type") in _ENRICHABLE_TYPES)
    enriched = sum(
        1 for _, d in g.nodes(data=True) if d.get("type") in _ENRICHABLE_TYPES and d.get("purpose")
    )
    return {"total": total, "enriched": enriched}


def apply_enrichment(g: nx.MultiDiGraph, entries: dict[str, dict]) -> int:
    """entries: {node_id: {"purpose": str, "tags": [str, ...]?, "domain": str?}}.
    `domain` only applies to File nodes (silently ignored on Class nodes --
    a class's domain is inherited from its file, not set independently).
    Silently skips unknown node ids or nodes of a non-enrichable type -- an
    enrichment payload from a stale graph shouldn't crash the merge."""
    applied = 0
    for node_id, fields in entries.items():
        if not g.has_node(node_id):
            continue
        node = g.nodes[node_id]
        if node.get("type") not in _ENRICHABLE_TYPES:
            continue
        fields = fields or {}
        purpose = fields.get("purpose")
        if not purpose:
            continue
        node["purpose"] = purpose
        tags = fields.get("tags")
        if tags:
            node["tags"] = tags
        domain = fields.get("domain")
        if domain and node.get("type") == "File":
            node["domain"] = domain
            # Classes/functions defined in this file inherit its (now
            # overridden) domain too, so graph coloring and any future
            # per-node domain lookups stay consistent with the file's.
            dotted = node.get("label")
            for other_id, other in g.nodes(data=True):
                if other.get("defined_in") == dotted:
                    other["domain"] = domain
        applied += 1
    return applied
