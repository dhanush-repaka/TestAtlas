"""Generic graph-shape stats -- schema-agnostic, works over any graph this
project builds (currently just the dev-code schema in kg/dev_graph_builder.py;
findings specific to that schema live in kg/dev_queries.py instead)."""
from __future__ import annotations

import networkx as nx


def graph_stats(g: nx.MultiDiGraph) -> dict:
    node_counts: dict[str, int] = {}
    for _, data in g.nodes(data=True):
        node_counts[data["type"]] = node_counts.get(data["type"], 0) + 1
    edge_counts: dict[str, int] = {}
    for _, _, data in g.edges(data=True):
        edge_counts[data["relation"]] = edge_counts.get(data["relation"], 0) + 1
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "by_node_type": dict(sorted(node_counts.items())),
        "by_relation": dict(sorted(edge_counts.items())),
    }
