"""Shared load/save helpers for the persisted per-run graph.json. Used by
anything that needs to read a finished run's graph back into networkx
(diffing, enrichment) without re-running analysis."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx


def load_graph(graph_path: str | Path) -> nx.MultiDiGraph:
    data = json.loads(Path(graph_path).read_text())
    return nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")


def save_graph(g: nx.MultiDiGraph, graph_path: str | Path) -> None:
    data = nx.node_link_data(g, edges="edges")
    Path(graph_path).write_text(json.dumps(data, default=str))
