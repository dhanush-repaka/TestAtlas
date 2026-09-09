"""Sync a networkx graph into Neo4j (AuraDB Free, or any Neo4j 5.x instance),
scoped by repo_id + run_id, so it can be browsed visually (Neo4j Browser /
Aura Console) and queried with real Cypher -- what plain networkx can't give
you: point-and-click exploration of the actual graph shape.

Centrality (PageRank / betweenness) is computed locally with networkx at
sync time and written onto each node as a plain property (`pagerank`,
`betweenness`) -- not via Neo4j's Graph Data Science plugin. GDS isn't
available on AuraDB Free/Professional (only the separate, paid AuraDS
product), so this keeps the whole pipeline on the free tier: Neo4j here is
purely storage + a browsing/query surface, exactly as it was originally
meant to be, not a compute engine we also have to run and babysit.

Entirely optional: if NEO4J_URI isn't set, every function here is a no-op /
returns empty, and the rest of TestAtlas behaves exactly as it did before
Neo4j existed. No hard dependency on a database being present.
"""
from __future__ import annotations

import os
import re
import time

import networkx as nx

_driver = None

# Only these become real Neo4j labels / relationship types -- both originate
# from dev_graph_builder.py's own fixed vocabulary, never external input, but the
# check stays as defense in depth since Cypher can't parameterize labels.
_ALLOWED_LABELS = {"Module", "Class", "Function"}
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_configured() -> bool:
    return bool(os.environ.get("NEO4J_URI"))


def get_driver():
    global _driver
    if _driver is not None:
        return _driver
    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return None
    from neo4j import GraphDatabase  # imported lazily so the package is only required when configured

    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver


def quick_ready_check() -> bool:
    """Fast check for interactive endpoints. AuraDB Free auto-pauses after a
    few days of inactivity and resumes on the next connection attempt --
    unlike the old self-hosted-on-Fly setup, there's no separate "wake" API
    to call first; a resume just makes the first attempt or two slower."""
    driver = get_driver()
    if driver is None:
        return False
    try:
        driver.verify_connectivity()
        return True
    except Exception:  # noqa: BLE001 -- any connectivity problem means "not ready yet"
        return False


def wait_until_ready(timeout_seconds: int = 60) -> bool:
    """Blocking version for the sync path: retries with backoff, covering an
    Aura instance resuming from a paused state."""
    driver = get_driver()
    if driver is None:
        return False
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            driver.verify_connectivity()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return False


def _serialize_props(d: dict) -> dict:
    """Neo4j properties must be primitives or homogeneous lists of primitives."""
    out = {}
    for k, v in d.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _local_centrality(g: nx.MultiDiGraph) -> tuple[dict, dict]:
    """PageRank + betweenness over the whole graph, computed with networkx --
    the same algorithms GDS would run, just free and local. Degenerate graphs
    (no edges, missing numpy/scipy) fall back to all-zero rather than failing
    the sync."""
    try:
        pagerank = nx.pagerank(g) if g.number_of_edges() else {n: 0.0 for n in g.nodes}
    except Exception:  # noqa: BLE001
        pagerank = {n: 0.0 for n in g.nodes}
    try:
        betweenness = (
            nx.betweenness_centrality(nx.DiGraph(g)) if g.number_of_edges() else {n: 0.0 for n in g.nodes}
        )
    except Exception:  # noqa: BLE001
        betweenness = {n: 0.0 for n in g.nodes}
    return pagerank, betweenness


def sync_graph(g: nx.MultiDiGraph, repo_id: str, run_id: str) -> None:
    """Idempotent: safe to call more than once for the same run_id (replaces it).

    Deliberately only waits a short window for Neo4j to be ready -- "Run
    analysis" is a synchronous HTTP request. If Aura isn't already warm
    (paused instance resuming), this skips the sync for this run (a warning
    is logged by the caller) rather than blocking; the Insights tab's own
    polling can afford to wait longer on the next check.
    """
    driver = get_driver()
    if driver is None:
        return

    if not wait_until_ready(timeout_seconds=10):
        raise RuntimeError(
            "Neo4j wasn't already warm and a resume can take a little while on a paused "
            "AuraDB Free instance -- try the Insights tab shortly, or re-run."
        )

    pagerank, betweenness = _local_centrality(g)
    _BATCH_SIZE = 500  # rows per UNWIND -- keeps individual transactions modest
                        # in size while still cutting round-trips from O(n) to O(n/500)

    def _chunks(items: list, size: int):
        for i in range(0, len(items), size):
            yield items[i : i + size]

    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT graphnode_uid IF NOT EXISTS FOR (n:GraphNode) REQUIRE n.uid IS UNIQUE"
        )
        # Idempotent re-sync: drop whatever this run_id previously wrote first.
        session.run("MATCH (n:GraphNode {run_id: $run_id}) DETACH DELETE n", run_id=run_id)

        # Grouped by label and batched via UNWIND: a few round-trips total
        # instead of one Cypher call per node/edge, which is what made
        # syncing even a few-hundred-node graph to a remote Aura instance
        # take tens of seconds -- each individual query pays real network
        # latency, and that adds up fast at O(nodes + edges) calls.
        nodes_by_label: dict[str, list[dict]] = {}
        for node_id, data in g.nodes(data=True):
            node_type = data.get("type", "Node")
            label = node_type if node_type in _ALLOWED_LABELS else "Node"
            props = _serialize_props(data)
            props["pagerank"] = round(pagerank.get(node_id, 0.0), 6)
            props["betweenness"] = round(betweenness.get(node_id, 0.0), 6)
            props.update(uid=f"{run_id}::{node_id}", id=node_id, repo_id=repo_id, run_id=run_id, type=node_type)
            nodes_by_label.setdefault(label, []).append(props)

        for label, rows in nodes_by_label.items():
            for chunk in _chunks(rows, _BATCH_SIZE):
                session.run(
                    f"""
                    UNWIND $rows AS row
                    CREATE (n:GraphNode:`{label}` {{uid: row.uid}})
                    SET n += row
                    """,
                    rows=chunk,
                )

        edges_by_type: dict[str, list[dict]] = {}
        for u, v, data in g.edges(data=True):
            relation = data.get("relation", "RELATED")
            rel_type = relation if _SAFE_IDENT.match(relation) else "RELATED"
            edges_by_type.setdefault(rel_type, []).append({"uid_a": f"{run_id}::{u}", "uid_b": f"{run_id}::{v}"})

        for rel_type, rows in edges_by_type.items():
            for chunk in _chunks(rows, _BATCH_SIZE):
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:GraphNode {{uid: row.uid_a}}), (b:GraphNode {{uid: row.uid_b}})
                    CREATE (a)-[r:`{rel_type}` {{run_id: $run_id}}]->(b)
                    """,
                    rows=chunk, run_id=run_id,
                )
