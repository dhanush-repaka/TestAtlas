"""Render the graph as an interactive HTML page (pyvis) and export graphml/json.

Layout: force-directed physics (barnesHut), not a fixed hierarchy -- a
dev-code graph isn't a clean DAG (imports and calls go every direction,
including back on themselves), so pinning nodes to strict tiers just produced
three straight, crossed-over lanes. Physics lets modules and their own
classes/functions pull into natural clusters instead, which is both more
honest about the graph's real shape and easier to read at a glance.

Color: each node is shaded by its business-module `domain` (see
kg/dev_graph_builder.py) -- a deterministic hue per domain (hashed from its
name), with File nodes darkest/biggest and their Classes/Functions
progressively lighter/smaller. Everything belonging to one business module
(Accounts, Payments, ...) reads as one visual cluster of the same hue, even
when it's split across several files, reinforcing what physics already pulls
together structurally.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
from pathlib import Path

import networkx as nx
from pyvis.network import Network

_BASE_SIZE = {"File": 26, "Class": 15, "Function": 9}
_BASE_LIGHTNESS = {"File": 0.40, "Class": 0.52, "Function": 0.62}

EDGE_STYLE = {
    "IMPORTS": ("#8e44ad", 0.55),
    "DEFINES": ("#c9cedb", 0.3),
    "CALLS": ("#2980b9", 0.6),
}
_DEFAULT_EDGE_STYLE = ("#b9bfd0", 0.4)


def _domain_color(domain_name: str, lightness: float) -> str:
    """Deterministic, evenly-distributed hue per domain name -- same domain
    always gets the same color across runs, no fixed palette to run out of."""
    digest = hashlib.md5(domain_name.encode()).hexdigest()
    hue = int(digest[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(hue, lightness, 0.55)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def to_pyvis_html(g: nx.MultiDiGraph, out_path: Path, height: str = "900px", banner_html: str | None = None) -> None:
    net = Network(
        height=height, width="100%", directed=True, notebook=False,
        cdn_resources="in_line", bgcolor="#0b0d14", font_color="#e7e9f2",
    )

    for node_id, data in g.nodes(data=True):
        node_type = data.get("type", "Unknown")
        domain_name = data.get("domain") or ""
        title_lines = [f"<b>{node_type}</b>: {data.get('label', node_id)}"]
        for k, v in data.items():
            if k in ("type", "label"):
                continue
            title_lines.append(f"{k}: {v}")

        color = _domain_color(domain_name, _BASE_LIGHTNESS.get(node_type, 0.55)) if domain_name else "#5b6172"

        # A node pulled from Neo4j (kg/graph_intelligence.py's fetch_graph_for_run)
        # carries a live PageRank score -- when present, let it drive size
        # instead of the flat per-type default, so this view visibly reflects
        # what Neo4j computed rather than just re-drawing local structure.
        neo4j_score = data.get("neo4j_pagerank")
        if neo4j_score is not None:
            size = 8 + min(38, float(neo4j_score) * 300)
        else:
            size = _BASE_SIZE.get(node_type, 8)

        net.add_node(
            node_id,
            label=data.get("label", node_id)[:34],
            title="<br>".join(str(t) for t in title_lines),
            color={"background": color, "border": color, "highlight": {"background": color, "border": "#ffffff"}},
            shape="dot",
            size=size,
            font={"size": 14 if node_type == "File" else 10, "color": "#e7e9f2", "face": "Inter, -apple-system, sans-serif"},
            borderWidth=1,
        )

    for u, v, data in g.edges(data=True):
        relation = data.get("relation", "")
        color, opacity = EDGE_STYLE.get(relation, _DEFAULT_EDGE_STYLE)
        net.add_edge(u, v, title=relation, arrows="to", color={"color": color, "opacity": opacity}, width=1)

    net.set_options(
        """
        {
          "layout": { "improvedLayout": false },
          "physics": {
            "solver": "barnesHut",
            "barnesHut": {
              "gravitationalConstant": -12000,
              "centralGravity": 0.25,
              "springLength": 90,
              "springConstant": 0.03,
              "damping": 0.25,
              "avoidOverlap": 0.4
            },
            "stabilization": { "enabled": true, "iterations": 300, "fit": true }
          },
          "edges": {
            "smooth": { "type": "continuous" },
            "arrows": { "to": { "enabled": true, "scaleFactor": 0.4 } }
          },
          "nodes": { "shadow": { "enabled": true, "size": 6, "x": 0, "y": 2 } },
          "interaction": { "hover": true, "tooltipDelay": 120, "navigationButtons": true, "keyboard": true }
        }
        """
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(out_path), notebook=False, open_browser=False)

    html = out_path.read_text()
    # Freeze physics once the initial settle finishes -- a graph of hundreds
    # of nodes left permanently simulating is a real CPU cost in the browser
    # for no benefit once it's already settled; this keeps the one-time
    # "cool" organic-clustering animation without it running forever.
    html = html.replace(
        "network = new vis.Network(container, data, options);",
        "network = new vis.Network(container, data, options);\n"
        "                  network.once('stabilizationIterationsDone', function () { network.setOptions({ physics: false }); });",
    )
    if banner_html:
        html = html.replace(
            "<body>",
            f"<body><div style=\"position:fixed;top:0;left:0;right:0;z-index:1000;"
            f"background:#161a26;color:#e7e9f2;padding:10px 18px;font:13px -apple-system,sans-serif;"
            f"display:flex;align-items:center;gap:10px;border-bottom:1px solid #2a2f42;\">{banner_html}</div>"
            f"<div style=\"height:44px\"></div>",
            1,
        )
    out_path.write_text(html)


def export_graphml(g: nx.MultiDiGraph, out_path: Path) -> None:
    clean = nx.MultiDiGraph()
    for n, d in g.nodes(data=True):
        clean.add_node(n, **{k: (",".join(v) if isinstance(v, list) else v) for k, v in d.items()})
    for u, v, d in g.edges(data=True):
        clean.add_edge(u, v, **{k: (",".join(v) if isinstance(v, list) else v) for k, v in d.items()})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(clean, out_path)


def export_json(g: nx.MultiDiGraph, out_path: Path) -> None:
    data = nx.node_link_data(g, edges="edges")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, default=str))
