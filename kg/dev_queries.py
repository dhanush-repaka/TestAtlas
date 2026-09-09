"""Findings over the dev-code graph (kg/dev_graph_builder.py). Parallels
kg/queries.py's test-schema findings, using the Module/Class/Function schema
instead.
"""
from __future__ import annotations

import networkx as nx


def unused_functions(g: nx.MultiDiGraph) -> list[str]:
    """Top-level functions and methods never called from anywhere we traced.

    Honest limitation: the mechanical CALLS resolver only follows direct
    name calls, self.method() calls, and imported-alias calls it can prove --
    dynamic dispatch, decorators-as-registries, and framework-invoked entry
    points (e.g. `main()`, route handlers, pytest fixtures) will show up here
    even though they're genuinely used. Treat this as "not provably used by
    static analysis", not "safe to delete".
    """
    unused = []
    for n, data in g.nodes(data=True):
        if data.get("type") != "Function":
            continue
        if data.get("name") == "__init__":
            continue
        if not any(d.get("relation") == "CALLS" for _, _, d in g.in_edges(n, data=True)):
            unused.append(n)
    return unused


def isolated_modules(g: nx.MultiDiGraph) -> list[str]:
    """Modules with no import edge in or out -- either genuinely standalone
    (e.g. a script or config module) or an entry point nothing internal
    references. Not necessarily a problem, but worth surfacing."""
    isolated = []
    for n, data in g.nodes(data=True):
        if data.get("type") != "Module":
            continue
        has_import_edge = any(
            d.get("relation") == "IMPORTS" for _, _, d in g.out_edges(n, data=True)
        ) or any(
            d.get("relation") == "IMPORTS" for _, _, d in g.in_edges(n, data=True)
        )
        if not has_import_edge:
            isolated.append(n)
    return isolated


def empty_classes(g: nx.MultiDiGraph) -> list[str]:
    """Classes with no methods -- often markers, exceptions, or dataclasses;
    surfaced as a finding rather than filtered out, since that distinction
    matters and the graph alone can't tell them apart."""
    empty = []
    for n, data in g.nodes(data=True):
        if data.get("type") != "Class":
            continue
        if not any(d.get("relation") == "DEFINES" for _, _, d in g.out_edges(n, data=True)):
            empty.append(n)
    return empty


def dev_findings(g: nx.MultiDiGraph) -> list[dict]:
    findings: list[dict] = []

    def add(category: str, node_ids):
        for n in node_ids:
            findings.append({"category": category, "node_id": n, "label": g.nodes[n].get("label", n)})

    add("unused_function", unused_functions(g))
    add("isolated_module", isolated_modules(g))
    add("empty_class", empty_classes(g))
    skipped = g.graph.get("skipped_files") or []
    for f in skipped:
        findings.append({"category": "unparsed_file", "node_id": f"file:{f}", "label": f})
    return findings
