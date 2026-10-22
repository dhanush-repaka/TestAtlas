from __future__ import annotations

import html
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import networkx as nx
import llm_logging
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from ado import client as ado_client
from github import client as github_client
from kg import embeddings, neo4j_sync
from kg.doc_gaps import ALLOWED_GAP_CATEGORIES, candidate_modules_context, gap_analysis_context, module_test_context
from kg.enrichment import apply_enrichment, enrichment_coverage, enrichment_targets
from kg.dev_graph_builder import score_modules
from kg.graph_intelligence import most_critical_nodes, bottleneck_nodes, fetch_graph_for_run
from kg.graph_io import load_graph, save_graph
from kg.journeys import detect_journeys, name_journeys
from kg.repo_parser import has_source_files
from kg.source_lookup import (
    gather_module_sources, gather_journey_sources,
    find_citation_domain_and_path, resolve_citation_node_id,
)
from kg.visualize import to_pyvis_html
from . import (
    auth, db, doc_extract, folder_picker, llm_gap_analysis, llm_journey_requirements,
    llm_journey_test_generation, llm_module_naming, llm_requirement_test_generation,
    llm_story_coverage, llm_test_generation, llm_test_verification, test_export, uploads,
    work_item_import,
)
from .diff import diff_runs
from .runner import resolve_source_dir, run_analysis

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Set BASE_PATH (e.g. "/testatlas") when this sits behind a reverse proxy that
# forwards the full path unchanged rather than stripping a prefix -- the same
# shape as a Next.js `basePath`. Empty locally, so http://localhost:8123/ just works.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

app = FastAPI(title="TestAtlas")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(auth.AuthMiddleware, base_path=BASE_PATH)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    if not auth.password_configured():
        print(
            "\n *** TESTATLAS_PASSWORD is not set -- running with NO LOGIN GATE. ***\n"
            " *** Fine for local use; set TESTATLAS_PASSWORD before exposing this publicly. ***\n"
        )


# --------------------------------------------------------------------------- login

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TestAtlas — Sign in</title>
<style>
  /* Kept in sync by hand with static/styles.css's :root token values -- this page can't
     load the main stylesheet (it's served before login, deliberately self-contained so
     a broken main stylesheet can never lock someone out of the login form itself). */
  body {{ display:flex; align-items:center; justify-content:center; height:100vh; margin:0;
         background:#0a0c11; color:#f1f3f8; font-family:-apple-system,sans-serif; }}
  form {{ background:#12151d; border:1px solid #272e40; border-radius:14px; padding:28px; width:280px;
          box-shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 24px -16px rgba(0,0,0,.55); }}
  h1 {{ font-size:17px; font-weight:800; margin:0 0 16px; display:flex; align-items:center; gap:8px; }}
  h1 .mark {{ color:#22d3c7; filter:drop-shadow(0 0 7px rgba(34,211,199,.35)); display:inline-flex; }}
  input {{ width:100%; box-sizing:border-box; background:#181c27; border:1px solid #272e40; color:#f1f3f8;
           border-radius:8px; padding:9px 11px; font-size:14px; margin-bottom:12px; font-family:inherit; }}
  button {{ width:100%; background:#22d3c7; border:none; color:#061412; font-weight:700;
            padding:10px; border-radius:8px; cursor:pointer; font-size:14px;
            box-shadow:0 1px 1px rgba(0,0,0,.08), 0 8px 20px -8px rgba(34,211,199,.35); }}
  button:hover {{ background:#5ee8de; }}
  .err {{ color:#ff6b6b; font-size:12px; margin:-4px 0 12px; }}
</style></head><body>
<form method="post" action="{login_path}">
  <h1><span class="mark"><svg viewBox="0 0 24 24" width="18" height="18" fill="none"><path d="M3 12c0-4.97 4.03-9 9-9 3.5 0 6.53 2 8 5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M21 12c0 4.97-4.03 9-9 9-3.5 0-6.53-2-8-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".45"/><circle cx="12" cy="12" r="2.4" fill="currentColor"/><circle cx="20" cy="8" r="2" fill="currentColor"/><circle cx="4" cy="16" r="2" fill="currentColor" opacity=".45"/></svg></span>TestAtlas</h1>
  {error_html}
  <input type="password" name="password" placeholder="Password" autofocus required>
  {next_field}
  <button type="submit">Sign in</button>
</form>
</body></html>"""


@app.get(f"{BASE_PATH}/login", response_class=HTMLResponse)
def login_page(error: bool = False, next_path: str = Query("", alias="next")):
    error_html = '<div class="err">Wrong password.</div>' if error else ""
    # `next` only ever reaches here as attacker-controlled input (an unauthenticated
    # visitor's own query string) -- carried through as a hidden field so it survives
    # the POST, escaped for safe use inside an HTML attribute, validated for real
    # (same-app path only) once it comes back in login_submit below.
    next_field = f'<input type="hidden" name="next" value="{html.escape(next_path)}">' if next_path else ""
    return LOGIN_PAGE.format(login_path=f"{BASE_PATH}/login", error_html=error_html, next_field=next_field)


@app.post(f"{BASE_PATH}/login")
async def login_submit(request: Request):
    form = await request.form()
    next_raw = str(form.get("next") or "")
    if auth.check_password(form.get("password", "")):
        resp = RedirectResponse(url=auth.safe_next_path(next_raw, BASE_PATH), status_code=303)
        resp.set_cookie(
            auth.COOKIE_NAME, auth.make_session_token(),
            max_age=auth.SESSION_TTL_SECONDS, httponly=True, samesite="lax",
        )
        return resp
    retry_url = f"{BASE_PATH}/login?error=1"
    if next_raw:
        retry_url += f"&next={quote(next_raw, safe='')}"
    return RedirectResponse(url=retry_url, status_code=303)


@app.get(f"{BASE_PATH}/logout")
def logout():
    """Drops back to read-only browsing rather than the login form -- signing
    out isn't the same as being locked out, and the site is still fully
    viewable without a session (see auth.AuthMiddleware)."""
    resp = RedirectResponse(url=f"{BASE_PATH}/", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# --------------------------------------------------------------------------- schemas

class RepoIn(BaseModel):
    name: str
    source_type: str  # 'local' | 'ado_git' | 'github_git' | 'upload' (files sent via POST /repos/{id}/upload-files)
    local_path: Optional[str] = None
    ado_url: Optional[str] = None  # e.g. https://dev.azure.com/{org}/{project}/_git/{repo}
    ado_branch: Optional[str] = "main"
    ado_pat: Optional[str] = None
    github_url: Optional[str] = None  # e.g. https://github.com/{owner}/{repo}
    github_branch: Optional[str] = "main"
    github_pat: Optional[str] = None  # optional -- only needed for private repos


class RepoUpdate(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    local_path: Optional[str] = None
    ado_url: Optional[str] = None
    ado_branch: Optional[str] = None
    ado_pat: Optional[str] = None
    github_url: Optional[str] = None
    github_branch: Optional[str] = None
    github_pat: Optional[str] = None


class TestConnectionIn(BaseModel):
    ado_url: str
    ado_pat: str


class GithubTestConnectionIn(BaseModel):
    github_url: str
    github_pat: Optional[str] = None


def _expand_ado_url(payload: dict) -> dict:
    """Replaces payload['ado_url'] (if present) with ado_org/ado_project/ado_repo."""
    url = payload.pop("ado_url", None)
    if url:
        try:
            parsed = ado_client.parse_repo_url(url)
        except ado_client.InvalidAdoUrlError as e:
            raise HTTPException(400, str(e))
        payload["ado_org"], payload["ado_project"], payload["ado_repo"] = (
            parsed["org"], parsed["project"], parsed["repo"],
        )
    return payload


def _expand_github_url(payload: dict) -> dict:
    """Replaces payload['github_url'] (if present) with github_owner/github_repo."""
    url = payload.pop("github_url", None)
    if url:
        try:
            parsed = github_client.parse_repo_url(url)
        except github_client.InvalidGithubUrlError as e:
            raise HTTPException(400, str(e))
        payload["github_owner"], payload["github_repo"] = parsed["owner"], parsed["repo"]
    return payload


# --------------------------------------------------------------------------- API routes
# All mounted under {BASE_PATH}/api so this deploys unchanged whether it's at
# http://localhost:8123/ or reverse-proxied at dhanushrepaka.com/testatlas.

api = APIRouter(prefix=f"{BASE_PATH}/api")


@api.get("/llm-calls")
def api_llm_calls(limit: int = 200):
    """Recent OpenAI calls across every feature -- latency, token usage, and
    which feature made the call (llm_logging.py's `site` tag) -- for
    diagnosing cost/latency/context questions without reading server logs by
    hand. Not scoped to a repo: this is a deployment-wide diagnostic, same
    spirit as the server's own stdout log, just structured and queryable."""
    return llm_logging.read_recent(limit)


@api.get("/repos")
def api_list_repos():
    return db.list_repos()


@api.post("/repos")
def api_create_repo(payload: RepoIn):
    if payload.source_type not in ("local", "ado_git", "github_git", "upload"):
        raise HTTPException(400, "source_type must be 'local', 'ado_git', 'github_git', or 'upload'")
    if payload.source_type == "local" and not payload.local_path:
        raise HTTPException(400, "local_path is required for source_type='local'")
    if payload.source_type == "ado_git" and not all([payload.ado_url, payload.ado_pat]):
        raise HTTPException(400, "ado_url and ado_pat are required for source_type='ado_git'")
    if payload.source_type == "github_git" and not payload.github_url:
        raise HTTPException(400, "github_url is required for source_type='github_git'")
    data = _expand_github_url(_expand_ado_url(payload.model_dump()))
    return db.create_repo(data)


@api.put("/repos/{repo_id}")
def api_update_repo(repo_id: str, payload: RepoUpdate):
    data = _expand_github_url(_expand_ado_url({k: v for k, v in payload.model_dump().items() if v is not None}))
    updated = db.update_repo(repo_id, data)
    if not updated:
        raise HTTPException(404, "repo not found")
    return updated


@api.delete("/repos/{repo_id}")
def api_delete_repo(repo_id: str):
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    db.delete_repo(repo_id)
    if repo["source_type"] == "upload":
        uploads.remove(repo_id)  # the uploaded snapshot has no other owner -- don't leave it on the volume
    return {"ok": True}


@api.post("/repos/{repo_id}/upload-files")
async def api_upload_repo_files(repo_id: str, files: list[UploadFile] = File(...), reset: bool = Form(False)):
    """Receives one batch of a browser-side folder upload (source_type
    'upload' repos only). Each part's *filename* carries the file's path
    relative to the chosen folder. Sent in batches because a real repo can
    have more files than one multipart request will accept; `reset` is set on
    a re-upload's first batch to replace the previous snapshot. See
    server/uploads.py for what's accepted and why it's strict."""
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    if repo["source_type"] != "upload":
        raise HTTPException(400, "this repo's source isn't an uploaded folder")

    items: list[tuple[str, bytes]] = []
    oversized = 0
    for f in files:
        if f.size is not None and f.size > uploads.MAX_FILE_BYTES:
            oversized += 1  # counted, not read into memory
            continue
        items.append((f.filename or "", await f.read()))
    try:
        result = await run_in_threadpool(uploads.save_files, repo_id, items, reset)
    except uploads.UploadTooLarge as e:
        raise HTTPException(413, str(e))
    if oversized:
        result["skipped"]["too_large"] = result["skipped"].get("too_large", 0) + oversized
    return result


@api.post("/repos/{repo_id}/test-connection")
def api_test_connection(repo_id: str):
    repo = db.get_repo(repo_id, include_pat=True)
    if not repo:
        raise HTTPException(404, "repo not found")
    if repo["source_type"] == "local":
        p = Path(repo["local_path"]).expanduser()
        if not p.is_dir():
            return {"ok": False, "message": f"'{p}' does not exist or is not a directory"}
        has_source = has_source_files(p)
        return {
            "ok": has_source,
            "message": f"Found source files (Python/TypeScript/JavaScript): {'yes' if has_source else 'none'}",
        }
    if repo["source_type"] == "upload":
        count, size = uploads.dir_stats(uploads.upload_dir(repo_id))
        return {
            "ok": count > 0,
            "message": f"{count:,} source file{'s' if count != 1 else ''} uploaded ({size / 1024:,.0f} KB)"
            if count else "Nothing uploaded yet -- open Settings and choose a folder to upload.",
        }
    if repo["source_type"] == "github_git":
        ok, message = github_client.test_connection(
            repo["github_owner"], repo["github_repo"], repo.get("github_pat") or None
        )
        return {"ok": ok, "message": message}
    ok, message = ado_client.test_connection(repo["ado_org"], repo["ado_project"], repo["ado_pat"])
    return {"ok": ok, "message": message}


@api.post("/ado/test-connection")
def api_ado_test_connection_raw(payload: TestConnectionIn):
    """Test a repo URL + PAT before a repo row even exists yet (used by the 'Add repo' form)."""
    try:
        parsed = ado_client.parse_repo_url(payload.ado_url)
    except ado_client.InvalidAdoUrlError as e:
        return {"ok": False, "message": str(e)}
    ok, message = ado_client.test_connection(parsed["org"], parsed["project"], payload.ado_pat)
    return {"ok": ok, "message": message}


@api.post("/github/test-connection")
def api_github_test_connection_raw(payload: GithubTestConnectionIn):
    """Test a GitHub repo URL (+ optional PAT for private repos) before a repo
    row even exists yet (used by the 'Add repo' form)."""
    try:
        parsed = github_client.parse_repo_url(payload.github_url)
    except github_client.InvalidGithubUrlError as e:
        return {"ok": False, "message": str(e)}
    ok, message = github_client.test_connection(parsed["owner"], parsed["repo"], payload.github_pat or None)
    return {"ok": ok, "message": message}


@api.post("/repos/{repo_id}/run")
def api_run(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return run_analysis(repo_id)


@api.get("/repos/{repo_id}/runs")
def api_list_runs(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.list_runs(repo_id)


@api.get("/runs/{run_id}")
def api_get_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@api.get("/runs/{run_id}/graph.html", response_class=HTMLResponse)
def api_get_run_graph_html(run_id: str):
    run = db.get_run(run_id)
    if not run or not run.get("html_path"):
        raise HTTPException(404, "graph not available for this run")
    return FileResponse(run["html_path"])


@api.get("/runs/{run_id}/graph.json")
def api_get_run_graph_json(run_id: str):
    run = db.get_run(run_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(404, "graph not available for this run")
    return FileResponse(run["graph_path"], media_type="application/json")


def _message_page(text: str) -> HTMLResponse:
    return HTMLResponse(
        f"<div style='display:flex;align-items:center;justify-content:center;height:100vh;"
        f"font:14px -apple-system,sans-serif;color:#888;text-align:center;padding:24px;'>{text}</div>"
    )


@api.get("/runs/{run_id}/neo4j-graph.html", response_class=HTMLResponse)
def api_get_run_neo4j_graph_html(run_id: str):
    """Renders the graph exactly as Neo4j has it right now (queried live from
    Aura, not the locally cached graph.json) -- node size reflects the
    PageRank score stored on each node, proving this view is actually backed
    by Neo4j rather than a re-display of the local copy."""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if not neo4j_sync.is_configured():
        return _message_page("Neo4j isn't configured for this deployment.")
    if not neo4j_sync.quick_ready_check():
        return _message_page("Waking up the graph database — this can take up to a minute the first time. Try again shortly.")

    data = fetch_graph_for_run(run_id)
    if not data:
        return _message_page("This run hasn't been synced to Neo4j yet — re-run analysis with Neo4j configured.")

    g = nx.MultiDiGraph()
    for node_id, props in data["nodes"]:
        g.add_node(node_id, **props)
    for u, v, relation in data["edges"]:
        if g.has_node(u) and g.has_node(v):
            g.add_edge(u, v, relation=relation)

    browser_url = _aura_browser_url()
    banner = (
        f"⬡ Live from Neo4j Aura — {g.number_of_nodes()} nodes, {g.number_of_edges()} edges "
        f"queried straight from the database just now (node size = stored PageRank)."
        + (f' <a href="{browser_url}" target="_blank" rel="noopener" '
           f'style="color:#8ab4ff;margin-left:auto;white-space:nowrap;">Open in Neo4j Browser ↗</a>' if browser_url else "")
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "neo4j_graph.html"
        to_pyvis_html(g, tmp_path, banner_html=banner)
        return HTMLResponse(tmp_path.read_text())


@api.get("/repos/{repo_id}/compare")
def api_compare(repo_id: str, baseline: str, current: str):
    run_a, run_b = db.get_run(baseline), db.get_run(current)
    if not run_a or not run_b:
        raise HTTPException(404, "one or both runs not found")
    if run_a["repo_id"] != repo_id or run_b["repo_id"] != repo_id:
        raise HTTPException(400, "both runs must belong to the given repo")
    try:
        return diff_runs(run_a, run_b)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _aura_browser_url() -> Optional[str]:
    """Deep-links into Neo4j's own hosted Browser, pre-filling just the
    connection URI -- never credentials, those still get typed in by hand."""
    uri = os.environ.get("NEO4J_URI")
    return f"https://browser.neo4j.io/?connectURL={uri}" if uri else None


@api.get("/runs/{run_id}/insights")
def api_run_insights(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if not neo4j_sync.is_configured():
        return {"configured": False, "warming_up": False, "most_critical": [], "bottlenecks": [], "browser_url": None}

    if not neo4j_sync.quick_ready_check():
        return {"configured": True, "warming_up": True, "most_critical": [], "bottlenecks": [], "browser_url": None}

    return {
        "configured": True,
        "warming_up": False,
        "most_critical": most_critical_nodes(run_id),
        "bottlenecks": bottleneck_nodes(run_id),
        "browser_url": _aura_browser_url(),
    }


class EnrichmentEntry(BaseModel):
    purpose: str
    tags: Optional[list[str]] = None
    domain: Optional[str] = None  # File nodes only -- overrides the mechanical default business-module grouping


class EnrichmentIn(BaseModel):
    entries: dict[str, EnrichmentEntry]


@api.get("/runs/{run_id}/enrichment")
def api_get_enrichment_targets(run_id: str):
    """What an LLM pass (Claude Code itself, or a subagent -- see
    kg/enrichment.py) should read and summarize next: every File/Class in
    this run's graph that doesn't have a `purpose` yet."""
    run = db.get_run(run_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(404, "run not found, or has no graph yet")
    g = load_graph(run["graph_path"])
    return {"targets": enrichment_targets(g), **enrichment_coverage(g)}


@api.post("/runs/{run_id}/enrichment")
def api_apply_enrichment(run_id: str, payload: EnrichmentIn):
    """Merges purpose summaries onto this run's graph and re-renders it --
    additive only, no structural change, so it's safe to call repeatedly as
    more summaries come in."""
    run = db.get_run(run_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(404, "run not found, or has no graph yet")
    g = load_graph(run["graph_path"])
    applied = apply_enrichment(g, {k: v.model_dump() for k, v in payload.entries.items()})
    save_graph(g, run["graph_path"])
    if run.get("html_path"):
        to_pyvis_html(g, Path(run["html_path"]))
    stats = run.get("stats") or {}
    stats["enrichment"] = enrichment_coverage(g)
    if "module_scores" in stats:  # dev-code run -- refresh so purposes show up without a re-run
        stats["module_scores"] = score_modules(g)
    db.update_run_stats(run_id, stats)
    return {"applied": applied, **stats["enrichment"]}


# --------------------------------------------------------------------------- documents & gap analysis

class DocumentIn(BaseModel):
    name: str
    content: str  # a high-level process/architecture doc covering the system overall,
                  # not scoped to one module -- gap analysis compares it against the whole graph


class DocumentUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None


class GapFinding(BaseModel):
    category: str
    description: str


class GapFindingsIn(BaseModel):
    findings: list[GapFinding]


class StoryRequirementIn(BaseModel):
    text: str
    type: str
    status: str
    covers: list[str] = []
    rationale: Optional[str] = None


class StoryRequirementsIn(BaseModel):
    requirements: list[StoryRequirementIn]


class ConfirmRequirementIn(BaseModel):
    status: str  # "supported" | "contradicted" -- a human resolves the ambiguity one way or the other, never back to unknown
    rationale: Optional[str] = None


@api.get("/repos/{repo_id}/documents")
def api_list_documents(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.list_documents(repo_id)


@api.post("/repos/{repo_id}/documents")
def api_create_document(repo_id: str, payload: DocumentIn):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.create_document(repo_id, payload.model_dump())


@api.put("/documents/{doc_id}")
def api_update_document(doc_id: str, payload: DocumentUpdate):
    updated = db.update_document(doc_id, {k: v for k, v in payload.model_dump().items() if v is not None})
    if not updated:
        raise HTTPException(404, "document not found")
    return updated


@api.post("/repos/{repo_id}/documents/upload")
async def api_upload_document(repo_id: str, file: UploadFile = File(...), name: Optional[str] = Form(None)):
    """Accepts an actual document file -- .docx, .pptx, .xlsx, .pdf, .md, or
    .txt -- and stores its extracted text as a new document. Extraction is
    local/pure-Python (kg/doc_extract... see server/doc_extract.py), no
    external service involved."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "file too large (max 20MB)")
    try:
        content = doc_extract.extract_text(file.filename or "", data)
    except doc_extract.UnsupportedDocumentError as e:
        raise HTTPException(400, str(e))
    doc_name = name or (file.filename.rsplit(".", 1)[0] if file.filename else "Untitled document")
    return db.create_document(repo_id, {"name": doc_name, "content": content})


@api.put("/documents/{doc_id}/upload")
async def api_upload_document_replace(doc_id: str, file: UploadFile = File(...), name: Optional[str] = Form(None)):
    """Same as creating from a file, but replaces an existing document's
    content -- used when editing a document by re-uploading a new file."""
    if not db.get_document(doc_id):
        raise HTTPException(404, "document not found")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "file too large (max 20MB)")
    try:
        content = doc_extract.extract_text(file.filename or "", data)
    except doc_extract.UnsupportedDocumentError as e:
        raise HTTPException(400, str(e))
    updates = {"content": content}
    if name:
        updates["name"] = name
    return db.update_document(doc_id, updates)


@api.post("/repos/{repo_id}/documents/import-work-items")
async def api_import_work_items(repo_id: str, file: UploadFile = File(...)):
    """Splits an ADO-style work-item export (.csv/.xlsx) into one document
    per row instead of one document for the whole export -- so each story
    hits the exact same proven per-document pipeline (Story Coverage, test
    generation) as a hand-authored document, rather than asking one decompose
    call to untangle dozens of unrelated stories. See server/work_item_import.py.
    Falls back to a single whole-file document (today's upload behavior) when
    no recognizable header row is found, same as split_work_items documents."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "file too large (max 20MB)")
    try:
        text = doc_extract.extract_text(file.filename or "", data)
    except doc_extract.UnsupportedDocumentError as e:
        raise HTTPException(400, str(e))
    items = work_item_import.split_work_items(text)
    if not items:
        raise HTTPException(400, "no content found in that file")
    fallback_name = file.filename.rsplit(".", 1)[0] if file.filename else "Untitled document"
    return [
        db.create_document(repo_id, {"name": item["name"] or fallback_name, "content": item["content"]})
        for item in items
    ]


@api.delete("/documents/{doc_id}")
def api_delete_document(doc_id: str):
    if not db.get_document(doc_id):
        raise HTTPException(404, "document not found")
    db.delete_document(doc_id)
    return {"ok": True}


@api.get("/repos/{repo_id}/gap-analysis-context")
def api_gap_analysis_context(repo_id: str):
    """What an LLM pass (Claude Code itself, or a subagent -- see
    kg/doc_gaps.py) needs to compare this repo's documents -- ALL of them
    combined, not one at a time -- against the whole codebase's real
    contents: every document's content plus every module's real
    files/classes/functions/purpose summaries, from the repo's latest
    successful run."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    docs = db.list_documents(repo_id)
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    context = gap_analysis_context(g, docs)
    if not context["ok"]:
        raise HTTPException(400, context["message"])
    context["run_id"] = run["id"]
    return context


@api.post("/repos/{repo_id}/gap-analysis/run")
def api_run_gap_analysis(repo_id: str):
    """Runs gap analysis automatically via a live OpenAI API call -- the one
    part of TestAtlas with a real, metered cost per use, opt-in via
    OPENAI_API_KEY (see server/llm_gap_analysis.py). Compares ALL of this
    repo's documents together in one pass and replaces the repo's stored
    findings with the result, same as the manual paste-findings flow."""
    if not llm_gap_analysis.is_configured():
        raise HTTPException(
            400,
            "Automatic analysis isn't configured on this deployment (no OPENAI_API_KEY) -- "
            "use \"Get analysis context\" + paste findings instead.",
        )
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    docs = db.list_documents(repo_id)
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    context = gap_analysis_context(g, docs)
    if not context["ok"]:
        raise HTTPException(400, context["message"])
    try:
        findings = llm_gap_analysis.run_gap_analysis(context)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.replace_repo_gap_findings(repo_id, run["id"], [d["name"] for d in docs], findings)
    return {"applied": len(findings), "findings": findings}


@api.post("/repos/{repo_id}/gap-findings")
def api_post_gap_findings(repo_id: str, payload: GapFindingsIn):
    """Stores the result of a gap-analysis comparison -- replaces this
    repo's previous findings, since a fresh pass (over all documents
    combined) supersedes the last one."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    for f in payload.findings:
        if f.category not in ALLOWED_GAP_CATEGORIES:
            raise HTTPException(400, f"invalid category '{f.category}' -- must be one of {sorted(ALLOWED_GAP_CATEGORIES)}")
    docs = db.list_documents(repo_id)
    run = db.get_latest_successful_run(repo_id)
    db.replace_repo_gap_findings(
        repo_id, run["id"] if run else None, [d["name"] for d in docs], [f.model_dump() for f in payload.findings]
    )
    return {"stored": len(payload.findings)}


@api.get("/repos/{repo_id}/gap-findings")
def api_get_gap_findings(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.list_repo_gap_findings(repo_id)


@api.get("/gap-analysis/config")
def api_gap_analysis_config():
    """Lets the UI know whether the automatic (paid) analysis button should
    show at all -- it's opt-in per deployment, off unless OPENAI_API_KEY is set."""
    return {"automatic_available": llm_gap_analysis.is_configured()}


# --------------------------------------------------------------------------- story coverage
#
# Compares ONE user-story document against the whole codebase -- unlike gap
# analysis, which always bundles every document together (see kg/doc_gaps.py's
# module docstring: that's to avoid false "undocumented" findings across
# documents, which doesn't apply here -- "how much of THIS story is built" is
# inherently a per-document question).

def _get_doc_in_repo(repo_id: str, doc_id: str) -> dict:
    doc = db.get_document(doc_id)
    if not doc or doc["repo_id"] != repo_id:
        raise HTTPException(404, "document not found")
    return doc


def _story_coverage_context(repo_id: str, doc: dict) -> tuple[dict, dict, object]:
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    context = gap_analysis_context(g, [doc], include_ui_text=True)
    if not context["ok"]:
        raise HTTPException(400, context["message"])
    return context, run, g


def _with_tested_status(repo_id: str, requirements: list[dict]) -> list[dict]:
    return llm_story_coverage.compute_tested_status(requirements, db.list_test_cases(repo_id))


@api.get("/repos/{repo_id}/documents/{doc_id}/story-coverage-context")
def api_story_coverage_context(repo_id: str, doc_id: str):
    """What an LLM pass needs to compare ONE story document against the
    whole codebase's real contents -- the free manual-fallback path, works
    with no OPENAI_API_KEY configured."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    doc = _get_doc_in_repo(repo_id, doc_id)
    context, run, _g = _story_coverage_context(repo_id, doc)
    context["run_id"] = run["id"]
    return context


@api.post("/repos/{repo_id}/documents/{doc_id}/story-coverage/run")
def api_run_story_coverage(repo_id: str, doc_id: str):
    """Runs Story Coverage via a live OpenAI API call -- gated on
    OPENAI_API_KEY, same opt-in exception as gap analysis and test
    generation. One combined call decomposes this document into acceptance
    criteria AND judges each against the whole codebase. Replaces this
    document's previously stored requirements (source='document' rows only)."""
    if not llm_story_coverage.is_configured():
        raise HTTPException(
            400,
            "Automatic story coverage isn't configured on this deployment (no OPENAI_API_KEY) -- "
            "use \"Get coverage context\" + paste requirements instead.",
        )
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    doc = _get_doc_in_repo(repo_id, doc_id)
    context, run, g = _story_coverage_context(repo_id, doc)
    source_root = resolve_source_dir(repo)
    index_path = Path(run["graph_path"]).parent / "embeddings.json"
    try:
        result = llm_story_coverage.generate_story_requirements(doc, context, g, source_root, index_path)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.replace_story_requirements(repo_id, doc_id, run["id"], result.requirements)
    return {
        "applied": len(result.requirements), "dropped": result.dropped,
        "requirements": _with_tested_status(repo_id, result.requirements),
    }


def _story_coverage_bulk_worker(job_id: str, repo_id: str, doc_ids: list[str], run: dict, g, source_root: Path) -> None:
    """Runs in a background daemon thread, looping this repo's documents ONE
    AT A TIME -- concurrency already happens inside generate_story_requirements's
    own per-requirement judge loop, so a second layer of parallelism here
    would only add simultaneous DB writers without a real throughput gain.
    One document's failure is recorded as that document's own result entry
    and never aborts the rest of the job."""
    index_path = Path(run["graph_path"]).parent / "embeddings.json"
    for doc_id in doc_ids:
        doc = db.get_document(doc_id)
        if not doc:
            db.update_bulk_job_progress(job_id, "(deleted)", {
                "document_id": doc_id, "document_name": None, "ok": False,
                "detail": "document was deleted during this run",
            })
            continue
        try:
            context = gap_analysis_context(g, [doc], include_ui_text=True)
            if not context["ok"]:
                raise RuntimeError(context["message"])
            result = llm_story_coverage.generate_story_requirements(doc, context, g, source_root, index_path)
            db.replace_story_requirements(repo_id, doc_id, run["id"], result.requirements)
            detail = f"{len(result.requirements)} requirement(s)"
            if result.dropped:
                detail += f", {len(result.dropped)} dropped"
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": True, "detail": detail,
            })
        except RuntimeError as e:
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": False, "detail": str(e),
            })
    db.finish_bulk_job(job_id, "success")


@api.post("/repos/{repo_id}/story-coverage/run-all")
def api_run_story_coverage_all(repo_id: str):
    """Starts a background job that runs Story Coverage for every document in
    this repo -- the bulk-import counterpart to api_run_story_coverage, for a
    CSV/Excel import that produced tens or hundreds of documents at once.
    Returns the job id immediately (the loop runs on a daemon thread); poll
    GET .../story-coverage/jobs/{job_id} for progress."""
    if not llm_story_coverage.is_configured():
        raise HTTPException(400, "Automatic story coverage isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    documents = db.list_documents(repo_id)
    if not documents:
        raise HTTPException(400, "this repo has no documents yet")
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    source_root = resolve_source_dir(repo)
    job = db.create_bulk_job(repo_id, "story_coverage", len(documents))
    threading.Thread(
        target=_story_coverage_bulk_worker,
        args=(job["id"], repo_id, [d["id"] for d in documents], run, g, source_root),
        daemon=True,
    ).start()
    return job


@api.get("/repos/{repo_id}/story-coverage/jobs/{job_id}")
def api_get_story_coverage_job(repo_id: str, job_id: str):
    job = db.get_bulk_job(job_id)
    if not job or job["repo_id"] != repo_id:
        raise HTTPException(404, "job not found")
    return job


@api.post("/repos/{repo_id}/documents/{doc_id}/story-requirements")
def api_post_story_requirements(repo_id: str, doc_id: str, payload: StoryRequirementsIn):
    """Stores the result of a manual (free) comparison pass -- same
    replace-scoped-to-this-document semantics as the automatic run."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    doc = _get_doc_in_repo(repo_id, doc_id)
    for r in payload.requirements:
        if r.type not in llm_story_coverage.ALLOWED_REQUIREMENT_TYPES:
            raise HTTPException(400, f"invalid type '{r.type}' -- must be one of {sorted(llm_story_coverage.ALLOWED_REQUIREMENT_TYPES)}")
        if r.status not in llm_story_coverage.ALLOWED_REQUIREMENT_STATUSES:
            raise HTTPException(400, f"invalid status '{r.status}' -- must be one of {sorted(llm_story_coverage.ALLOWED_REQUIREMENT_STATUSES)}")
    context, run, _g = _story_coverage_context(repo_id, doc)
    validated = llm_story_coverage.validate_requirements(context, [r.model_dump() for r in payload.requirements])
    db.replace_story_requirements(repo_id, doc_id, run["id"], validated)
    return {"stored": len(validated)}


@api.get("/repos/{repo_id}/documents/{doc_id}/story-requirements")
def api_get_story_requirements(repo_id: str, doc_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    _get_doc_in_repo(repo_id, doc_id)
    return _with_tested_status(repo_id, db.list_story_requirements(repo_id, doc_id))


@api.get("/story-coverage/config")
def api_story_coverage_config():
    """Lets the UI know whether the automatic (paid) story coverage button
    should show at all -- opt-in per deployment, off unless OPENAI_API_KEY is set."""
    return {"automatic_available": llm_story_coverage.is_configured()}


@api.post("/repos/{repo_id}/documents/{doc_id}/story-requirements/{requirement_id}/generate-test-case")
def api_generate_test_case_from_requirement(repo_id: str, doc_id: str, requirement_id: str):
    """Generates 1-or-more test cases directly from ONE acceptance criterion's
    own wording -- ungrounded: no code is shown, and no resolved citation is
    required up front. Stored with status=NULL, covers=[], module=NULL until
    a later "Verify against code" pass judges it (see
    api_verify_test_cases_for_document). See server/llm_requirement_test_generation.py."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    requirement = db.get_story_requirement(requirement_id)
    if not requirement or requirement["doc_id"] != doc_id:
        raise HTTPException(404, "requirement not found")
    try:
        cases = llm_requirement_test_generation.generate_test_cases_from_requirement(requirement)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    run = db.get_latest_successful_run(repo_id)  # optional provenance only -- not required to generate
    run_id = run["id"] if run else None
    return [
        db.create_test_case(repo_id, None, run_id, case, source_requirement_id=requirement_id, source_doc_id=doc_id)
        for case in cases
    ]


@api.post("/repos/{repo_id}/documents/{doc_id}/story-requirements/generate-test-cases")
def api_generate_test_cases_from_document(repo_id: str, doc_id: str, replace: bool = False):
    """Generates the full realistic set of test cases directly from this
    document's own raw story text (name + content) -- ungrounded, no code
    shown, no requirement that Story Coverage has ever run on this document.
    Stored with status=NULL, covers=[], module=NULL until a later "Verify
    against code" pass judges them (see api_verify_test_cases_for_document).
    `replace=true` (opt-in, never the default -- generation is additive
    otherwise, same as every other generator in this app) deletes this
    document's previously-generated cases first, so a regenerate doesn't pile
    duplicates on top of an earlier pass.
    See server/llm_requirement_test_generation.py's generate_test_cases_from_document."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    doc = _get_doc_in_repo(repo_id, doc_id)
    try:
        cases = llm_requirement_test_generation.generate_test_cases_from_document(doc)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if replace:
        db.delete_test_cases_by_source_doc(repo_id, doc_id)
    run = db.get_latest_successful_run(repo_id)  # optional provenance only -- not required to generate
    run_id = run["id"] if run else None
    return [db.create_test_case(repo_id, None, run_id, case, source_doc_id=doc_id) for case in cases]


def _test_generation_bulk_worker(job_id: str, repo_id: str, doc_ids: list[str], run_id: str | None, replace: bool) -> None:
    """Background counterpart to api_generate_test_cases_from_document --
    ungrounded, so unlike the pre-fix version this needs no graph/source_root
    at all. A document with no content is recorded as a failed entry, not a
    crash."""
    for doc_id in doc_ids:
        doc = db.get_document(doc_id)
        if not doc:
            db.update_bulk_job_progress(job_id, "(deleted)", {
                "document_id": doc_id, "document_name": None, "ok": False,
                "detail": "document was deleted during this run",
            })
            continue
        try:
            cases = llm_requirement_test_generation.generate_test_cases_from_document(doc)
            if replace:
                db.delete_test_cases_by_source_doc(repo_id, doc_id)
            created = [db.create_test_case(repo_id, None, run_id, case, source_doc_id=doc_id) for case in cases]
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": True,
                "detail": f"{len(created)} test case(s)",
            })
        except RuntimeError as e:
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": False, "detail": str(e),
            })
    db.finish_bulk_job(job_id, "success")


@api.post("/repos/{repo_id}/test-cases/generate-all-documents")
def api_generate_test_cases_all_documents(repo_id: str, replace: bool = False):
    """Starts a background job that generates test cases for every document
    in this repo, straight from each document's own story text -- the
    bulk-import counterpart to api_generate_test_cases_from_document.
    `replace=true` clears each document's own previously-generated cases
    right before its new batch is stored. Returns the job id immediately;
    poll GET .../test-cases/jobs/{job_id} for progress."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    documents = db.list_documents(repo_id)
    if not documents:
        raise HTTPException(400, "this repo has no documents yet")
    run = db.get_latest_successful_run(repo_id)  # optional provenance only -- not required to generate
    job = db.create_bulk_job(repo_id, "test_generation", len(documents))
    threading.Thread(
        target=_test_generation_bulk_worker,
        args=(job["id"], repo_id, [d["id"] for d in documents], run["id"] if run else None, replace),
        daemon=True,
    ).start()
    return job


@api.get("/repos/{repo_id}/test-cases/jobs/{job_id}")
def api_get_test_generation_job(repo_id: str, job_id: str):
    job = db.get_bulk_job(job_id)
    if not job or job["repo_id"] != repo_id:
        raise HTTPException(404, "job not found")
    return job


# --------------------------------------------------------------------------- document-scoped test-case verification
#
# Generation (above) never looks at code -- it writes the full realistic set
# of test cases straight from a document's own story text, stored unverified
# (status=NULL, covers=[], module=NULL). This is the second half: judges
# those already-written cases against the real codebase, same three-state
# grounding discipline (supported/contradicted/unknown, citation-validated,
# demote-on-fabricated-citation) used everywhere else in this app, reusing
# llm_test_verification.generate_test_case_verifications UNCHANGED -- only
# how context/sources get built (retrieval-narrowed across a whole batch of
# cases, not scoped to one module a UI button already named) differs from the
# existing module-scoped /test-cases/verify route.

def _case_embedding_text(c: dict) -> str:
    """One short blurb per test case for the retrieval step -- title plus
    each step's action/expected, same idea as kg/embeddings.py's own
    module-text building, just built purely from what the case already says
    about itself (no code involved)."""
    steps = c.get("steps") or []
    parts = [c.get("title") or ""]
    parts.extend(f"{s.get('action','')} {s.get('expected','')}" for s in steps if isinstance(s, dict))
    return " ".join(p for p in parts if p)


def _load_verification_run_state(repo_id: str) -> tuple:
    """Everything a batch of document-scoped Verify calls needs, loaded ONCE
    per request/bulk-job (not once per document) -- the graph, every
    document, the source root, the whole-codebase context (for building/
    loading the retrieval index), and the cached per-run embedding index
    itself. Raises RuntimeError, message safe to show the user, if there's
    no successful analysis run yet."""
    repo = db.get_repo(repo_id)
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise RuntimeError("this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    docs = db.list_documents(repo_id)
    source_root = resolve_source_dir(repo)
    full_context = gap_analysis_context(g, docs, require_docs=False, include_ui_text=False)
    if not full_context["ok"]:
        raise RuntimeError(full_context["message"])
    index_path = Path(run["graph_path"]).parent / "embeddings.json"
    index: list[dict] = []
    try:
        index = embeddings.load_index(index_path)
        if not index:
            index = embeddings.build_module_index(full_context)
            embeddings.save_index(index, index_path)
    except RuntimeError:
        index = []  # retrieval is an optimization, not a hard dependency -- same fallback discipline as Story Coverage
    return g, docs, source_root, full_context, index


def _candidate_modules_for_cases(index: list[dict], all_modules: list[str], cases: list[dict]) -> set[str]:
    """Embeds every case's own title+steps text in ONE batched call, retrieves
    each case's own top-K candidate modules, and unions them across the whole
    batch -- narrows a whole-codebase module search down to a short,
    real-source-backed shortlist instead of feeding every module's full
    source into one verify call. Falls back to every module when retrieval
    isn't available, same discipline as Story Coverage."""
    if not index:
        return set(all_modules)
    try:
        query_embeddings = embeddings.embed_texts([_case_embedding_text(c) for c in cases])
    except RuntimeError:
        return set(all_modules)
    out: set[str] = set()
    for q in query_embeddings:
        out.update(embeddings.retrieve_candidate_modules(index, q, k=10))
    return out


def _verify_cases_for_document(repo_id: str, g, docs: list[dict], source_root: Path, full_context: dict, index: list[dict], cases: list[dict]):
    """One document's (or one bulk-job document's) full Verify step: narrow
    candidates, gather real source for just that narrowed set, call
    llm_test_verification.generate_test_case_verifications ONCE, then patch
    each judged case's status/rationale/covers AND resolve+set its module
    for the first time (find_citation_domain_and_path, same helper used
    elsewhere) since it started with module=NULL."""
    all_modules = [m["module"] for m in full_context.get("modules") or []]
    candidate_modules = sorted(_candidate_modules_for_cases(index, all_modules, cases))
    sources = gather_module_sources(g, source_root, candidate_modules)
    context = candidate_modules_context(g, docs, candidate_modules)
    if not context["ok"]:
        raise RuntimeError(context["message"])
    result = llm_test_verification.generate_test_case_verifications(cases, context, sources)
    for v in result.results:
        module = None
        for name in v.get("covers") or []:
            found = find_citation_domain_and_path(g, name)
            if found:
                module = found[0]
                break
        db.update_test_case_verification(v["id"], v["status"], v.get("rationale"), v.get("covers") or [], module)
    return result


@api.post("/repos/{repo_id}/documents/{doc_id}/test-cases/verify")
def api_verify_test_cases_for_document(repo_id: str, doc_id: str):
    """Document-scoped sibling of api_verify_test_cases (module-scoped) --
    judges every test case tied to ONE document (via source_doc_id) against
    real retrieved source. Reuses llm_test_verification.generate_test_case_verifications
    unchanged -- only how context/sources get built differs."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case verification isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    _get_doc_in_repo(repo_id, doc_id)
    cases = [c for c in db.list_test_cases(repo_id) if c.get("source_doc_id") == doc_id]
    if not cases:
        raise HTTPException(400, "this document has no generated test cases yet -- generate some first")
    try:
        g, docs, source_root, full_context, index = _load_verification_run_state(repo_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    try:
        result = _verify_cases_for_document(repo_id, g, docs, source_root, full_context, index, cases)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"applied": len(result.results), "dropped": result.dropped, "test_cases": db.list_test_cases(repo_id)}


def _document_verification_bulk_worker(job_id: str, repo_id: str, doc_ids: list[str]) -> None:
    """Bulk counterpart to api_verify_test_cases_for_document -- loads the
    run/graph/index ONCE, then loops documents sequentially (same reasoning
    as _story_coverage_bulk_worker: concurrency, if any, belongs inside the
    per-batch call, not across documents)."""
    try:
        g, docs, source_root, full_context, index = _load_verification_run_state(repo_id)
    except RuntimeError as e:
        db.finish_bulk_job(job_id, "failed", str(e))
        return
    for doc_id in doc_ids:
        doc = db.get_document(doc_id)
        if not doc:
            db.update_bulk_job_progress(job_id, "(deleted)", {
                "document_id": doc_id, "document_name": None, "ok": False,
                "detail": "document was deleted during this run",
            })
            continue
        try:
            cases = [c for c in db.list_test_cases(repo_id) if c.get("source_doc_id") == doc_id]
            if not cases:
                raise RuntimeError("this document has no generated test cases yet")
            result = _verify_cases_for_document(repo_id, g, docs, source_root, full_context, index, cases)
            detail = f"{len(result.results)} case(s) verified"
            if result.dropped:
                detail += f", {len(result.dropped)} dropped"
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": True, "detail": detail,
            })
        except RuntimeError as e:
            db.update_bulk_job_progress(job_id, doc["name"], {
                "document_id": doc_id, "document_name": doc["name"], "ok": False, "detail": str(e),
            })
    db.finish_bulk_job(job_id, "success")


@api.post("/repos/{repo_id}/test-cases/verify-all-documents")
def api_verify_test_cases_all_documents(repo_id: str):
    """Bulk counterpart to api_verify_test_cases_for_document -- same
    one-job-per-repo, sequential-per-document shape as story-coverage/run-all
    and test-cases/generate-all-documents."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case verification isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    documents = db.list_documents(repo_id)
    if not documents:
        raise HTTPException(400, "this repo has no documents yet")
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    job = db.create_bulk_job(repo_id, "test_verification", len(documents))
    threading.Thread(
        target=_document_verification_bulk_worker,
        args=(job["id"], repo_id, [d["id"] for d in documents]),
        daemon=True,
    ).start()
    return job


@api.get("/repos/{repo_id}/test-cases/verify-jobs/{job_id}")
def api_get_test_verification_job(repo_id: str, job_id: str):
    job = db.get_bulk_job(job_id)
    if not job or job["repo_id"] != repo_id:
        raise HTTPException(404, "job not found")
    return job


# --------------------------------------------------------------------------- journeys

def _journey_member_requirements(repo_id: str, g, journey: dict) -> list[dict]:
    """A journey's requirements come from two directions (server/db.py's
    schema comment on `journeys`/`story_requirements.journey_id` has the full
    reasoning): rows DERIVED from this journey's own code (stored with
    journey_id set), union document-sourced rows whose resolved citations
    fall inside this journey's node_ids -- computed here, never stored, since
    a document-sourced requirement doesn't "belong" to one journey, it just
    happens to cite code that journey also reaches."""
    node_ids = set(journey.get("node_ids") or [])
    out = []
    for r in db.list_story_requirements(repo_id):
        if r.get("journey_id") == journey["id"]:
            out.append(r)
            continue
        if r.get("journey_id"):
            continue  # derived for a DIFFERENT journey -- not this one's member
        for name in r.get("covers") or []:
            node_id = resolve_citation_node_id(g, name)
            if node_id and node_id in node_ids:
                out.append(r)
                break
    return out


def _journey_run_and_graph(repo_id: str) -> tuple[dict, object]:
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    return run, load_graph(run["graph_path"])


@api.post("/repos/{repo_id}/journeys/detect")
def api_detect_journeys(repo_id: str):
    """Deterministic, free -- no LLM call except the one-shot naming pass
    (kg.journeys.name_journeys), which falls back to the route prefix itself
    on any failure. Upserts by stable journey id (see kg.journeys.detect_journeys's
    docstring for why) -- a prior journey's requirements/answers are never
    orphaned by re-running this."""
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    run, g = _journey_run_and_graph(repo_id)
    journeys = detect_journeys(g)
    names = name_journeys(g, journeys)
    for j in journeys:
        j["name"] = names.get(j["id"], j["prefix"])
    db.replace_journeys(repo_id, run["id"], journeys)
    return {"detected": len(journeys)}


@api.get("/repos/{repo_id}/journeys")
def api_list_journeys(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    journeys = db.list_journeys(repo_id)
    if not journeys:
        return []
    run = db.get_latest_successful_run(repo_id)
    g = load_graph(run["graph_path"]) if run and run.get("graph_path") else None
    out = []
    for j in journeys:
        members = _journey_member_requirements(repo_id, g, j) if g is not None else []
        statuses = [m["status"] for m in members]
        out.append({
            **j,
            "members": members,
            "member_count": len(members),
            "supported_count": statuses.count("supported"),
            "contradicted_count": statuses.count("contradicted"),
            "needs_human_input_count": statuses.count("needs_human_input"),
            "unknown_count": statuses.count("unknown"),
        })
    return out


@api.post("/repos/{repo_id}/journeys/{journey_id}/derive-requirements")
def api_derive_journey_requirements(repo_id: str, journey_id: str):
    """Finds candidate requirements directly in a journey's code -- for the
    case no user story exists yet (server/llm_journey_requirements.py).
    Replaces only this journey's previously DERIVED rows -- a human-confirmed
    answer for this same journey is never touched (db.replace_derived_requirements)."""
    if not llm_journey_requirements.is_configured():
        raise HTTPException(400, "Deriving requirements isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    journey = db.get_journey(journey_id)
    if not journey or journey["repo_id"] != repo_id:
        raise HTTPException(404, "journey not found")
    run, g = _journey_run_and_graph(repo_id)
    source_root = resolve_source_dir(repo)
    sources = gather_journey_sources(g, source_root, journey)
    try:
        requirements = llm_journey_requirements.generate_journey_requirements(journey["name"], sources)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.replace_derived_requirements(repo_id, journey_id, run["id"], requirements)
    return {"derived": len(requirements)}


@api.post("/repos/{repo_id}/story-requirements/{requirement_id}/confirm")
def api_confirm_story_requirement(repo_id: str, requirement_id: str, payload: ConfirmRequirementIn):
    """A human answers a needs_human_input question -- flips `source` to
    'human_confirmed' (server/db.py's confirm_story_requirement); from this
    moment the row behaves exactly like a document-sourced one everywhere
    else. This is the entire "answers compound over time" mechanism."""
    if payload.status not in ("supported", "contradicted"):
        raise HTTPException(400, "status must be 'supported' or 'contradicted' -- a human resolves the ambiguity, it doesn't stay open")
    requirement = db.get_story_requirement(requirement_id)
    if not requirement or requirement["repo_id"] != repo_id:
        raise HTTPException(404, "requirement not found")
    return db.confirm_story_requirement(requirement_id, payload.status, payload.rationale)


@api.post("/repos/{repo_id}/journeys/{journey_id}/generate-test-case")
def api_generate_journey_test_case(repo_id: str, journey_id: str):
    """Generates ONE end-to-end test case spanning a whole journey's already-
    judged member requirements (server/llm_journey_test_generation.py) --
    never re-judges any of them; each step is tagged with the requirement and
    status that backs it, and the case's overall status is the worst among
    its members (never a single fake collapsed verdict)."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    journey = db.get_journey(journey_id)
    if not journey or journey["repo_id"] != repo_id:
        raise HTTPException(404, "journey not found")
    run, g = _journey_run_and_graph(repo_id)
    members = _journey_member_requirements(repo_id, g, journey)
    if not members:
        raise HTTPException(400, "this journey has no requirements yet -- derive some from its code, or link a user story, first")
    source_root = resolve_source_dir(repo)
    sources = gather_journey_sources(g, source_root, journey)
    try:
        case = llm_journey_test_generation.generate_test_case_from_journey(journey["name"], members, sources)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    # No single module owns a cross-cutting journey -- the first member
    # requirement's resolved citation picks a reasonable home for the case,
    # same fallback shape api_generate_test_case_from_requirement already uses.
    module = None
    for r in members:
        for name in r.get("covers") or []:
            found = find_citation_domain_and_path(g, name)
            if found:
                module = found[0]
                break
        if module:
            break
    return db.create_test_case(repo_id, module, run["id"], case, source_journey_id=journey_id)


# --------------------------------------------------------------------------- LLM test case generation

@api.post("/repos/{repo_id}/test-cases/run")
def api_run_test_generation(repo_id: str, module: str):
    """Generates test cases via a live OpenAI API call, SCOPED TO ONE MODULE
    (see server/llm_test_generation.py) -- an earlier whole-repo version
    spread one call's ~59-case output-token budget across every module in
    the repo, leaving most modules with zero cases on anything but a small
    codebase. One call per module instead gives each module its own full
    budget, at the cost of one OpenAI call per module generated. Unlike gap
    analysis, documents are optional here -- code structure alone is enough
    to generate test cases; documents (if any) just add richer grounding
    for what's "critical" and what the intended behavior is. Also unlike gap
    analysis, this has no free manual fallback -- gated entirely on
    OPENAI_API_KEY, same as automatic gap analysis. Grounded in this
    module's REAL source (gather_module_sources) so a case's expected
    result describes what the code actually does, bugs included -- its
    status/rationale, if any, is derived separately (server/llm_story_coverage.py's
    link_requirements_to_cases) from whatever Story Coverage has already
    judged about the matching user story, not re-checked against this same
    code again. Replaces this module's previously stored test cases with the
    result -- other modules' cases are untouched."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    docs = db.list_documents(repo_id)
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    labels = db.get_module_label_details(repo_id)
    label = labels.get(module) or {}
    context = module_test_context(g, docs, module, display_name=label.get("name"), description=label.get("description"), labels=labels)
    if not context["ok"]:
        raise HTTPException(400, context["message"])
    try:
        source_root = resolve_source_dir(repo)
        sources = gather_module_sources(g, source_root, [module])
    except OSError:
        sources = {}  # degrade to name-only generation rather than fail the whole pass over a source-access problem
    try:
        result = llm_test_generation.generate_test_cases(context, sources=sources)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.replace_test_cases(repo_id, module, run["id"], result.cases)
    return {
        "applied": len(result.cases), "test_cases": result.cases, "module": module,
        # what the model wrote but we refused, and what the coverage pass did -- surfaced, never silent
        "dropped": result.dropped, "repaired": result.repaired, "still_uncovered": result.still_uncovered,
        "deepened": result.deepened, "still_thin": result.still_thin,
    }


@api.get("/repos/{repo_id}/test-cases")
def api_list_test_cases(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    cases = db.list_test_cases(repo_id)
    requirements = db.list_story_requirements(repo_id)  # every document's requirements, not just one
    return llm_story_coverage.link_requirements_to_cases(cases, requirements)


@api.delete("/repos/{repo_id}/test-cases")
def api_delete_test_cases_by_module(repo_id: str, module: str):
    """Manual cleanup for one module's whole bucket of test cases -- the
    "Clear" action next to the Module filter in the Test Cases tab. Scoped
    purely by the `module` string already stored on each case, regardless of
    origin (code/story/journey) or whether that module still exists in the
    latest analysis run's graph -- the main reason this exists is to clear
    cases left orphaned under a module name a repo's real structure has since
    moved past (a renamed or restructured module), which nothing else in the
    app can target."""
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    deleted = db.delete_test_cases_by_module(repo_id, module)
    return {"deleted": deleted}


@api.get("/repos/{repo_id}/test-cases/export")
def api_export_test_cases(repo_id: str, format: str = "csv", module: str = "", category: str = ""):
    """Downloads the repo's generated test cases as a file -- `csv` (Azure DevOps Test
    Plans import shape, opens in Excel), `md` (readable document) or `json`. `module`
    and `category` narrow it the same way the Test Cases tab's filters do."""
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    if format not in test_export.FORMATS:
        raise HTTPException(400, f"format must be one of: {', '.join(test_export.FORMATS)}")
    cases = [c for c in db.list_test_cases(repo_id)
             if (not module or c.get("module") == module) and (not category or c.get("category") == category)]
    if not cases:
        raise HTTPException(404, "no test cases to export -- generate some first")
    labels = db.get_module_label_details(repo_id)
    body = {"csv": lambda: test_export.to_csv(cases, labels),
            "md": lambda: test_export.to_markdown(cases, labels, repo["name"]),
            "json": lambda: test_export.to_json(cases, labels)}[format]()
    media_type, ext = test_export.FORMATS[format]
    return Response(body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{test_export.filename(repo["name"], ext)}"'})


@api.get("/test-generation/config")
def api_test_generation_config():
    """Lets the UI know whether the "Generate test cases" button should show
    at all -- opt-in per deployment, off unless OPENAI_API_KEY is set."""
    return {"automatic_available": llm_test_generation.is_configured()}


@api.get("/repos/{repo_id}/module-labels")
def api_get_module_labels(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.get_module_label_details(repo_id)   # {module: {name, kind, description}}


@api.post("/repos/{repo_id}/module-labels/generate")
def api_generate_module_labels(repo_id: str):
    """Names every module in this repo's latest run for a NON-TECHNICAL reader --
    the screen or feature it powers ("Shopping Cart", "Product Page"), whether it
    is a feature or works behind the scenes, and a one-sentence description --
    via live OpenAI calls (see server/llm_module_naming.py; batched for large
    repos). Purely cosmetic, the real dotted-path module name underneath is
    unchanged. Unlike test cases/gap findings,
    this doesn't replace anything: existing labels for modules the model
    renames again are just overwritten, and labels for modules that no
    longer exist are left in place (harmless, unused until that module
    reappears)."""
    if not llm_module_naming.is_configured():
        raise HTTPException(400, "Module naming isn't configured on this deployment (no OPENAI_API_KEY).")
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    run = db.get_latest_successful_run(repo_id)
    if not run or not run.get("graph_path"):
        raise HTTPException(400, "this repo has no successful analysis run yet -- run analysis first")
    g = load_graph(run["graph_path"])
    context = gap_analysis_context(g, [], require_docs=False, include_ui_text=True)  # on-screen text is the best naming clue
    if not context["ok"]:
        raise HTTPException(400, context["message"])
    try:
        labels = llm_module_naming.generate_module_names(context)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.set_module_labels(repo_id, labels)
    return {"applied": len(labels), "modules": labels}


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _folder_picker_allowed(request: Request) -> bool:
    """A native dialog opens on the machine the *server* runs on, so it's only
    meaningful -- and only offered -- when that's the same machine as the
    browser making the request. A deployed instance (Fly, a VPS) sees its
    clients arrive from a proxy/remote address, never loopback, so the
    button simply never appears there; a dialog popping up on a remote
    server's (nonexistent) screen would just hang the request."""
    return bool(request.client and request.client.host in _LOOPBACK_HOSTS and folder_picker.is_available())


@api.get("/system/config")
def api_system_config(request: Request):
    """Lets the UI know whether to show the Local-folder "Browse..." button,
    and whether this visitor is signed in -- an anonymous visitor on a
    password-protected deployment gets read-only access (AuthMiddleware
    enforces it server-side regardless), so the UI disables every button
    that would otherwise hit a write endpoint."""
    return {
        "folder_picker_available": _folder_picker_allowed(request),
        "login_required": auth.password_configured(),
        "authenticated": auth.is_authenticated(request),
    }


@api.post("/system/pick-folder")
def api_pick_folder(request: Request):
    """Opens the OS folder dialog on this machine and returns the chosen
    absolute path ({"path": null} if cancelled). Blocks until the user
    decides, hence a plain `def` -- FastAPI runs it in a worker thread, so
    other requests keep being served while the dialog is open."""
    if not _folder_picker_allowed(request):
        raise HTTPException(
            403,
            "Folder browsing only works when TestAtlas is running on the same machine you're using "
            "-- type or paste the path instead.",
        )
    try:
        return {"path": folder_picker.pick_folder()}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@api.get("/module-naming/config")
def api_module_naming_config():
    """Lets the UI know whether the "Generate friendly names" button should
    show at all -- opt-in per deployment, off unless OPENAI_API_KEY is set."""
    return {"automatic_available": llm_module_naming.is_configured()}


app.include_router(api)


# --------------------------------------------------------------------------- static SPA

app.mount(f"{BASE_PATH}/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_INDEX_HTML = (STATIC_DIR / "index.html").read_text().replace(
    "<!--BASE_HREF-->", f'<base href="{BASE_PATH}/">'
)


@app.get(f"{BASE_PATH}/", response_class=HTMLResponse)
def index():
    return _INDEX_HTML


if BASE_PATH:
    @app.get(BASE_PATH, response_class=HTMLResponse)
    def index_no_trailing_slash():
        # Browsers resolve the page's own <base href> against the URL they actually
        # loaded; without the trailing slash "api/repos" would resolve one level up
        # (stripping "testatlas" instead of appending to it), so redirect instead of
        # serving the same HTML at both.
        return RedirectResponse(url=f"{BASE_PATH}/")


@app.get(f"{BASE_PATH}/{{_rest:path}}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(_rest: str):
    """The front end now reads/writes real paths (static/app.js's tiny router --
    "/repo/<id>/<tab>"), so those need to resolve on a hard reload, a bookmark, or
    a shared link, not just when reached by clicking inside the already-loaded
    page. FastAPI/Starlette matches routes in registration order, so this always
    comes LAST: a request for anything registered above (/api/..., /static/...,
    /login, the bare "/") matches its own specific route first and never reaches
    here. Anything else still under this app's prefix -- including a stale or
    made-up repo id -- gets the same SPA shell; the client-side router decides
    what that path means and falls back to the dashboard if it doesn't resolve,
    exactly like an unmatched client-side route in any other SPA."""
    return _INDEX_HTML
