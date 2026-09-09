from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import networkx as nx
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ado import client as ado_client
from github import client as github_client
from kg import neo4j_sync
from kg.doc_gaps import ALLOWED_GAP_CATEGORIES, gap_analysis_context
from kg.enrichment import apply_enrichment, enrichment_coverage, enrichment_targets
from kg.dev_graph_builder import score_modules
from kg.graph_intelligence import most_critical_nodes, bottleneck_nodes, fetch_graph_for_run
from kg.graph_io import load_graph, save_graph
from kg.visualize import to_pyvis_html
from . import auth, db, doc_extract, llm_gap_analysis, llm_test_generation
from .diff import diff_runs
from .runner import run_analysis

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
  body {{ display:flex; align-items:center; justify-content:center; height:100vh; margin:0;
         background:#0c0e13; color:#eceef4; font-family:-apple-system,sans-serif; }}
  form {{ background:#14171f; border:1px solid #262c3c; border-radius:14px; padding:28px; width:280px; }}
  h1 {{ font-size:16px; margin:0 0 16px; }}
  input {{ width:100%; box-sizing:border-box; background:#1b1f2a; border:1px solid #262c3c; color:#eceef4;
           border-radius:8px; padding:9px 11px; font-size:14px; margin-bottom:12px; }}
  button {{ width:100%; background:#7c94ff; border:none; color:#0a0c12; font-weight:700;
            padding:10px; border-radius:8px; cursor:pointer; font-size:14px; }}
  .err {{ color:#ff7a7a; font-size:12px; margin:-4px 0 12px; }}
</style></head><body>
<form method="post" action="{login_path}">
  <h1>◆ TestAtlas</h1>
  {error_html}
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>
</form>
</body></html>"""


@app.get(f"{BASE_PATH}/login", response_class=HTMLResponse)
def login_page(error: bool = False):
    error_html = '<div class="err">Wrong password.</div>' if error else ""
    return LOGIN_PAGE.format(login_path=f"{BASE_PATH}/login", error_html=error_html)


@app.post(f"{BASE_PATH}/login")
async def login_submit(request: Request):
    form = await request.form()
    if auth.check_password(form.get("password", "")):
        resp = RedirectResponse(url=f"{BASE_PATH}/", status_code=303)
        resp.set_cookie(
            auth.COOKIE_NAME, auth.make_session_token(),
            max_age=auth.SESSION_TTL_SECONDS, httponly=True, samesite="lax",
        )
        return resp
    return RedirectResponse(url=f"{BASE_PATH}/login?error=1", status_code=303)


# --------------------------------------------------------------------------- schemas

class RepoIn(BaseModel):
    name: str
    source_type: str  # 'local' | 'ado_git' | 'github_git'
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


@api.get("/repos")
def api_list_repos():
    return db.list_repos()


@api.post("/repos")
def api_create_repo(payload: RepoIn):
    if payload.source_type not in ("local", "ado_git", "github_git"):
        raise HTTPException(400, "source_type must be 'local', 'ado_git', or 'github_git'")
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
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    db.delete_repo(repo_id)
    return {"ok": True}


@api.post("/repos/{repo_id}/test-connection")
def api_test_connection(repo_id: str):
    repo = db.get_repo(repo_id, include_pat=True)
    if not repo:
        raise HTTPException(404, "repo not found")
    if repo["source_type"] == "local":
        p = Path(repo["local_path"]).expanduser()
        if not p.is_dir():
            return {"ok": False, "message": f"'{p}' does not exist or is not a directory"}
        py_files = list(p.rglob("*.py"))[:1]
        return {
            "ok": bool(py_files),
            "message": f"Found .py files: {'yes' if py_files else 'none'}",
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


# --------------------------------------------------------------------------- LLM test case generation

@api.post("/repos/{repo_id}/test-cases/run")
def api_run_test_generation(repo_id: str):
    """Generates test cases via a live OpenAI API call, from the same
    graph+docs context gap analysis uses (see server/llm_test_generation.py).
    Unlike gap analysis this has no free manual fallback -- gated entirely on
    OPENAI_API_KEY, same as automatic gap analysis. Replaces this repo's
    stored test cases with the result."""
    if not llm_test_generation.is_configured():
        raise HTTPException(400, "Test case generation isn't configured on this deployment (no OPENAI_API_KEY).")
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
        cases = llm_test_generation.run_test_generation(context)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    db.replace_test_cases(repo_id, run["id"], cases)
    return {"applied": len(cases), "test_cases": cases}


@api.get("/repos/{repo_id}/test-cases")
def api_list_test_cases(repo_id: str):
    if not db.get_repo(repo_id):
        raise HTTPException(404, "repo not found")
    return db.list_test_cases(repo_id)


@api.get("/test-generation/config")
def api_test_generation_config():
    """Lets the UI know whether the "Generate test cases" button should show
    at all -- opt-in per deployment, off unless OPENAI_API_KEY is set."""
    return {"automatic_available": llm_test_generation.is_configured()}


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
