"""Orchestrates one analysis run for a repo config: resolve source -> parse
dev code -> build graph -> score modules -> compute findings -> persist."""
from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import networkx as nx

from ado import client as ado_client
from github import client as github_client
from kg import neo4j_sync
from kg.dev_graph_builder import build_dev_graph, score_modules
from kg.dev_queries import dev_findings
from kg.repo_parser import has_source_files, parse_repo
from kg.queries import graph_stats
from kg.visualize import to_pyvis_html

from . import db

# See server/db.py's DATA_ROOT comment -- same reasoning, so both dirs sit
# under one mountable volume.
WORKSPACE_DIR = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent.parent))) / "workspace"


def resolve_source_dir(repo: dict) -> Path:
    if repo["source_type"] == "local":
        path = Path(repo["local_path"]).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"local_path does not exist or is not a directory: {path}")
        return path

    if repo["source_type"] == "upload":
        # A folder the user uploaded from their browser (server/uploads.py) --
        # already sitting in this repo's workspace dir, no clone needed.
        dest = WORKSPACE_DIR / repo["id"]
        if not dest.is_dir() or not has_source_files(dest):
            raise RuntimeError(
                "No folder has been uploaded for this repo yet -- open Settings and choose a folder to upload."
            )
        return dest

    if repo["source_type"] == "ado_git":
        pat = repo.get("ado_pat") or ""
        if not pat:
            raise RuntimeError("no PAT stored for this repo")
        dest = WORKSPACE_DIR / repo["id"]
        ado_client.clone_or_update(
            org=repo["ado_org"],
            project=repo["ado_project"],
            repo=repo["ado_repo"],
            pat=pat,
            branch=repo.get("ado_branch") or "main",
            dest_dir=dest,
        )
        return dest

    if repo["source_type"] == "github_git":
        dest = WORKSPACE_DIR / repo["id"]
        github_client.clone_or_update(
            owner=repo["github_owner"],
            repo=repo["github_repo"],
            token=repo.get("github_pat") or None,
            branch=repo.get("github_branch") or "main",
            dest_dir=dest,
        )
        return dest

    raise RuntimeError(f"unknown source_type: {repo['source_type']}")


def run_analysis(repo_id: str) -> dict:
    repo = db.get_repo(repo_id, include_pat=True)
    if not repo:
        raise RuntimeError("repo not found")

    run = db.create_run(repo_id)
    run_id = run["id"]
    try:
        source_dir = resolve_source_dir(repo)

        # Mechanical layer: real syntax-tree parsing (Python's `ast`, tree-sitter for
        # TypeScript/JavaScript), not a framework-specific parser -- works on any
        # codebase in those languages regardless of what's built on top of it.
        # Purpose summaries/semantic enrichment are a separate pass layered on
        # after a run finishes (see kg/enrichment.py), not part of this step.
        parsed_repo = parse_repo(source_dir)
        g = build_dev_graph(parsed_repo)
        module_scores = score_modules(g)

        if g.number_of_nodes() == 0:
            # A real parser producing an empty graph is almost always "no source
            # files in this repo" or a bad source path -- fail loudly instead
            # of recording a hollow "success" that looks identical to a real,
            # tiny graph.
            raise RuntimeError(
                "Parsed 0 nodes -- no supported source files (Python, TypeScript or JavaScript) were "
                "found to parse as modules/classes/functions. Check the repo's source path/branch in Settings."
            )

        run_dir = db.RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        graph_path = run_dir / "graph.json"
        html_path = run_dir / "graph.html"

        data = nx.node_link_data(g, edges="edges")
        graph_path.write_text(json.dumps(data, default=str))
        to_pyvis_html(g, html_path)

        stats = graph_stats(g)
        stats["module_scores"] = module_scores
        stats["skipped_files"] = g.graph.get("skipped_files", [])
        findings = dev_findings(g)

        if neo4j_sync.is_configured():
            try:
                neo4j_sync.sync_graph(g, repo_id, run_id)
            except Exception as e:  # noqa: BLE001 -- a Neo4j hiccup shouldn't fail the whole run
                print(f"Neo4j sync failed for run {run_id}: {e}")

        db.finish_run(
            run_id,
            status="success",
            stats=stats,
            findings=findings,
            graph_path=str(graph_path),
            html_path=str(html_path),
        )
        return db.get_run(run_id)
    except Exception as e:  # noqa: BLE001 -- surface any failure into the run record
        db.finish_run(run_id, status="failed", error=f"{e}\n{traceback.format_exc()}")
        return db.get_run(run_id)
