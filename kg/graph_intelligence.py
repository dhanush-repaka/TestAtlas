"""Ranked-node queries over a run's graph, synced into Neo4j by
kg/neo4j_sync.py -- reading the PageRank/betweenness scores that were
computed locally with networkx at sync time and stored as plain node
properties (`pagerank`, `betweenness`). No Graph Data Science plugin
involved: GDS isn't available on AuraDB Free/Professional, so this stays on
the free tier by never calling it.

Every function returns [] if Neo4j isn't configured -- callers don't need to
branch on is_configured() themselves for read paths.
"""
from __future__ import annotations

from . import neo4j_sync


def _top_by(run_id: str, prop: str, top_n: int, threshold: float = 0.0) -> list[dict]:
    driver = neo4j_sync.get_driver()
    if driver is None:
        return []
    with driver.session() as session:
        result = session.run(
            f"""
            MATCH (n:GraphNode {{run_id: $run_id}})
            WHERE n.{prop} > $threshold
            RETURN n.id AS id, n.label AS label, n.type AS type, n.{prop} AS score
            ORDER BY score DESC
            LIMIT $top_n
            """,
            run_id=run_id, top_n=top_n, threshold=threshold,
        )
        return [dict(r) for r in result]


def most_critical_nodes(run_id: str, top_n: int = 10) -> list[dict]:
    """PageRank: nodes many other nodes structurally depend on, directly or
    transitively. High score = high blast radius if this node breaks or changes --
    e.g. a locator half the page objects route through, or a fixture every
    scenario ultimately relies on.

    No absolute score threshold: networkx's PageRank is probability-normalized
    (scores across the whole graph sum to 1), so "significant" is relative to
    graph size, not a fixed cutoff -- unlike GDS's unnormalized PageRank this
    used to read from, where a fixed 0.15 threshold meant something. Top-N by
    score is the honest version of "most critical" here."""
    return _top_by(run_id, "pagerank", top_n)


def bottleneck_nodes(run_id: str, top_n: int = 10) -> list[dict]:
    """Betweenness centrality: nodes sitting on the most shortest paths between
    other nodes -- a proxy for "if this breaks, the most other things lose
    their connection to what they depend on," distinct from PageRank's "most
    depended upon" (a node can be a bottleneck without being popular)."""
    return _top_by(run_id, "betweenness", top_n, threshold=0.0)


def fetch_graph_for_run(run_id: str) -> dict | None:
    """Pulls this run's synced graph straight out of Neo4j -- nodes and
    relationships, each node already carrying the pagerank/betweenness
    properties written at sync time -- so what renders in TestAtlas's "Neo4j
    graph" view is provably the graph Neo4j has, not just a re-display of the
    local networkx copy. Returns None if Neo4j isn't configured/reachable or
    this run was never synced."""
    driver = neo4j_sync.get_driver()
    if driver is None:
        return None

    with driver.session() as session:
        node_rows = session.run(
            "MATCH (n:GraphNode {run_id: $run_id}) RETURN properties(n) AS props",
            run_id=run_id,
        )
        nodes: list[tuple[str, dict]] = []
        for row in node_rows:
            props = dict(row["props"])
            node_id = props.pop("id", None)
            props.pop("uid", None)
            props.pop("run_id", None)
            props.pop("repo_id", None)
            if node_id:
                if "pagerank" in props:
                    props["neo4j_pagerank"] = props["pagerank"]
                nodes.append((node_id, props))

        if not nodes:
            return None

        edge_rows = session.run(
            """
            MATCH (a:GraphNode {run_id: $run_id})-[r]->(b:GraphNode {run_id: $run_id})
            RETURN a.id AS source, b.id AS target, type(r) AS relation
            """,
            run_id=run_id,
        )
        edges = [(row["source"], row["target"], row["relation"]) for row in edge_rows]

    return {"nodes": nodes, "edges": edges}
