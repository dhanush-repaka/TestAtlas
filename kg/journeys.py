"""Detects "journeys" -- end-to-end user flows traced from real HTTP entry
points through the existing call graph (kg/dev_graph_builder.py's CALLS
edges), clustered by shared route prefix. Mechanical and deterministic, the
same "assign a grouping key, let an LLM only refine the label" idiom
dev_graph_builder.py already uses for `domain` -- the clustering decision
itself never touches an LLM, only the human-readable name does.

An entry point is just a Function node with `is_entry_point=True` (set by
kg/python_ast_parser.py when it recognizes an HTTP route decorator --
`@app.get(...)`, `@router.post(...)`, FastAPI-shaped only for now: the only
framework this session's demo repos actually use. Flask's `@app.route(...,
methods=[...])` and non-Python frameworks are deliberately out of scope --
"start with whatever real customers actually run," same call the design
brief made for module detection's own LLM-override path.

Journeys don't replace `domain` grouping -- a journey can (and usually does)
span several domains, which is the whole point: "checkout" is realistically
split across cart, payment and address modules, and a journey is the one
place that's visible as a single flow instead of three disconnected ones.
"""
from __future__ import annotations

import json
import os
import re

import networkx as nx

from llm_logging import call_and_log

DEFAULT_MODEL = "gpt-4o-mini"


def _is_configured() -> bool:
    # Same check as server/llm_gap_analysis.py's is_configured(), duplicated
    # rather than imported -- kg/ stays a pure graph/parsing layer that
    # server/ depends on, never the other way around.
    return bool(os.environ.get("OPENAI_API_KEY"))
_MAX_SAMPLE_NAMES = 12


def _calls_subgraph(g: nx.MultiDiGraph) -> nx.DiGraph:
    """CALLS edges only, Function nodes only -- the same "filter edges, build
    a throwaway subgraph" idiom kg/dev_graph_builder.py's _file_centrality
    already uses, just scoped to a different relation."""
    sub = nx.DiGraph()
    sub.add_nodes_from(n for n, d in g.nodes(data=True) if d.get("type") == "Function")
    for u, v, d in g.edges(data=True):
        if d.get("relation") == "CALLS":
            sub.add_edge(u, v)
    return sub


def _route_prefix(path: str) -> str:
    """First real path segment: "/checkout/pay" -> "/checkout"; "/" stays "/"."""
    parts = [p for p in path.split("/") if p]
    return "/" + parts[0] if parts else "/"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "root"


def detect_journeys(g: nx.MultiDiGraph) -> list[dict]:
    """Deterministic, no LLM call. Returns a list of
    {id, prefix, entry_point_ids, node_ids} dicts.

    `id` is a STABLE slug of the route prefix (e.g. "journey:checkout"), never
    a fresh uuid -- re-running detection after a new analysis must upsert, not
    orphan, an existing story_requirements.journey_id reference (especially a
    human-confirmed one). See server/db.py's replace_journeys.

    Clustering is purely by shared first route-path segment today (the
    mechanical default, mirroring dev_graph_builder.py's _default_domain). A
    reachable-set-overlap-based refinement (merging two different prefixes
    whose code heavily overlaps, or splitting one prefix whose entry points
    don't actually share code) is a known, deliberately deferred improvement
    -- the design this follows explicitly expects this kind of clustering to
    be heuristic, shown as uncertain rather than asserted as settled."""
    entry_points = [
        (n, d) for n, d in g.nodes(data=True) if d.get("type") == "Function" and d.get("is_entry_point")
    ]
    if not entry_points:
        return []

    calls = _calls_subgraph(g)
    clusters: dict[str, dict] = {}
    for node_id, data in entry_points:
        routes = data.get("routes") or []
        prefixes = {_route_prefix(r["path"]) for r in routes if r.get("path")} or {"/"}
        reachable = set(nx.descendants(calls, node_id)) if node_id in calls else set()
        reachable.add(node_id)
        for prefix in prefixes:
            key = _slugify(prefix)
            bucket = clusters.setdefault(key, {
                "id": f"journey:{key}", "prefix": prefix, "entry_point_ids": set(), "node_ids": set(),
            })
            bucket["entry_point_ids"].add(node_id)
            bucket["node_ids"] |= reachable

    out = [
        {
            "id": bucket["id"],
            "prefix": bucket["prefix"],
            "entry_point_ids": sorted(bucket["entry_point_ids"]),
            "node_ids": sorted(bucket["node_ids"]),
        }
        for bucket in clusters.values()
    ]
    return sorted(out, key=lambda j: j["prefix"])


def _journey_clues(g: nx.MultiDiGraph, journey: dict) -> dict:
    routes: list[str] = []
    for ep_id in journey["entry_point_ids"]:
        data = g.nodes.get(ep_id) or {}
        for r in data.get("routes") or []:
            label = f"{r['method'].upper()} {r['path']}"
            if label not in routes:
                routes.append(label)
    labels: list[str] = []
    for node_id in journey["node_ids"]:
        data = g.nodes.get(node_id) or {}
        if data.get("type") in ("Function", "Class") and data.get("label") not in labels:
            labels.append(data["label"])
    return {"id": journey["id"], "routes": routes, "sample_names": labels[:_MAX_SAMPLE_NAMES]}


def _build_naming_prompt(clues: list[dict]) -> str:
    return f"""Name each of these detected user journeys -- a journey is a group of HTTP routes plus everything they call, traced from a codebase's real call graph. Name it the way a business analyst would describe the FLOW a user goes through, in Title Case, 2-5 words ("Checkout", "User Registration", "Password Reset") -- not a technical description of the routes.

JOURNEYS -- each has its real HTTP routes and a sample of real function/class names reachable from them:
---
{json.dumps(clues, indent=2)}
---

Respond with ONLY a JSON object of this exact shape, one entry per journey id listed above:
{{"journeys": {{"<id>": {{"name": "..."}}, ...}}}}"""


def name_journeys(g: nx.MultiDiGraph, journeys: list[dict], model: str | None = None) -> dict[str, str]:
    """One batched LLM call to name every detected journey -- the clustering
    itself (detect_journeys) never touches the model, only this label does.
    Falls back to the route prefix itself (e.g. "/checkout") as the name on
    any failure -- naming is cosmetic, never a hard dependency for the rest of
    this feature to work."""
    if not journeys:
        return {}
    fallback = {j["id"]: j["prefix"] for j in journeys}
    if not _is_configured():
        return fallback

    clues = [_journey_clues(g, j) for j in journeys]
    model = model or DEFAULT_MODEL
    prompt = _build_naming_prompt(clues)

    try:
        from openai import OpenAI  # lazy import: only needed when this is actually called

        response = call_and_log("journeys.name", model, lambda: OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        ), prompt_chars=len(prompt))
        parsed = json.loads(response.choices[0].message.content or "{}")
        entries = parsed.get("journeys", {})
        if not isinstance(entries, dict):
            return fallback
    except Exception:  # noqa: BLE001 -- naming is cosmetic; any failure here just falls back
        return fallback

    out = dict(fallback)
    for jid, value in entries.items():
        if jid in fallback and isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"].strip():
            out[jid] = value["name"].strip()
    return out
