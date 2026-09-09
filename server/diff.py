"""Compare two runs of the SAME repo: what changed in the graph (nodes/edges
added, removed, changed) and which findings were introduced or resolved.

Node ids are derived from stable names in the source (module.dotted.name,
Class.method, ...), so the same entity keeps the same id across runs as long
as it isn't renamed. That's what makes this diff meaningful instead of just
"everything looks new every time".
"""
from __future__ import annotations

import networkx as nx

from kg.graph_io import load_graph


def _edge_set(g: nx.MultiDiGraph) -> set[tuple[str, str, str]]:
    return {(u, v, d.get("relation", "")) for u, v, d in g.edges(data=True)}


def _brief(g: nx.MultiDiGraph, node_id: str) -> dict:
    d = g.nodes[node_id]
    out = {"id": node_id, "type": d.get("type"), "label": d.get("label")}
    if "module" in d:
        out["module"] = d["module"]
    return out


def diff_runs(run_a: dict, run_b: dict) -> dict:
    """run_a = baseline (older), run_b = current (newer)."""
    if not run_a.get("graph_path") or not run_b.get("graph_path"):
        raise ValueError("both runs must have completed successfully with a stored graph")

    ga = load_graph(run_a["graph_path"])
    gb = load_graph(run_b["graph_path"])

    nodes_a, nodes_b = set(ga.nodes), set(gb.nodes)
    added_ids = nodes_b - nodes_a
    removed_ids = nodes_a - nodes_b
    common_ids = nodes_a & nodes_b

    changed = []
    for n in common_ids:
        da, db = ga.nodes[n], gb.nodes[n]
        keys = set(da) | set(db)
        diffs = {k: [da.get(k), db.get(k)] for k in keys if da.get(k) != db.get(k)}
        if diffs:
            changed.append({**_brief(gb, n), "changes": diffs})

    edges_a, edges_b = _edge_set(ga), _edge_set(gb)
    added_edges = edges_b - edges_a
    removed_edges = edges_a - edges_b

    findings_a = {(f["category"], f["node_id"]) for f in (run_a.get("findings") or [])}
    findings_b = {(f["category"], f["node_id"]) for f in (run_b.get("findings") or [])}
    by_key_b = {(f["category"], f["node_id"]): f for f in (run_b.get("findings") or [])}
    by_key_a = {(f["category"], f["node_id"]): f for f in (run_a.get("findings") or [])}

    findings_new = [by_key_b[k] for k in (findings_b - findings_a)]
    findings_resolved = [by_key_a[k] for k in (findings_a - findings_b)]
    findings_persisting = [by_key_b[k] for k in (findings_a & findings_b)]

    return {
        "baseline_run": run_a["id"],
        "current_run": run_b["id"],
        "nodes_added": [_brief(gb, n) for n in sorted(added_ids)],
        "nodes_removed": [_brief(ga, n) for n in sorted(removed_ids)],
        "nodes_changed": changed,
        "edges_added": [{"from": u, "to": v, "relation": r} for u, v, r in sorted(added_edges)],
        "edges_removed": [{"from": u, "to": v, "relation": r} for u, v, r in sorted(removed_edges)],
        "findings_new": findings_new,
        "findings_resolved": findings_resolved,
        "findings_persisting": findings_persisting,
    }
