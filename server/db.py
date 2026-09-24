"""SQLite storage for repo configs and analysis runs. Sync/simple by design --
this is a single-user local tool, not a multi-tenant service (see README)."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import crypto

# DATA_ROOT lets one mounted volume host both data/ and workspace/ (Fly Machines,
# and most single-VM platforms, only support one volume per machine) -- unset
# locally, so this defaults to the repo root exactly as before.
_ROOT = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent.parent)))
DATA_DIR = _ROOT / "data"
DB_PATH = DATA_DIR / "kg.db"
RUNS_DIR = DATA_DIR / "runs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- 'local' | 'ado_git' | 'github_git' | 'upload'
    framework TEXT NOT NULL DEFAULT 'python_devcode',  -- reserved for future non-Python parsers
    local_path TEXT,
    ado_org TEXT,
    ado_project TEXT,
    ado_repo TEXT,
    ado_branch TEXT DEFAULT 'main',
    ado_pat_enc TEXT,
    github_owner TEXT,
    github_repo TEXT,
    github_branch TEXT DEFAULT 'main',
    github_pat_enc TEXT,              -- optional: only needed for private repos
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,             -- 'running' | 'success' | 'failed'
    error TEXT,
    stats_json TEXT,
    findings_json TEXT,
    graph_path TEXT,
    html_path TEXT,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

-- Project docs (READMEs, design notes, ...) -- repo-level, independent of any
-- one run: a design doc doesn't change every time the code gets re-analyzed.
-- Each doc is linked to the business module (domain) it describes, which is
-- what makes gap analysis possible (kg/dev_graph_builder.py's `domain`
-- rollup gives us "what does this module actually contain" to compare against).
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    domain TEXT,                      -- business module this doc describes, e.g. "kg" or "app.payments"
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

-- Gap findings from comparing a repo's documents -- ALL of them together, as
-- one corpus, not one at a time -- against what the codebase actually shows.
-- Produced by an LLM pass (see kg/doc_gaps.py) reading both sides -- not
-- computed here, this table just stores the result of that comparison.
-- (Superseded the earlier per-document doc_gap_findings table below, which
-- an existing production DB may still carry rows in but nothing reads anymore.)
CREATE TABLE IF NOT EXISTS repo_gap_findings (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    run_id TEXT,                      -- which run's graph was used as the comparison baseline
    category TEXT NOT NULL,           -- 'missing_implementation' | 'undocumented_capability' | 'mismatch'
    description TEXT NOT NULL,
    source_docs TEXT,                 -- comma-joined names of the documents compared in this pass
    created_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS doc_gap_findings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    run_id TEXT,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id)
);

-- LLM-generated FUNCTIONAL test cases in Azure DevOps shape (title, description,
-- priority, preconditions, steps each with an action + expected result) -- not runnable code,
-- produced by one live OpenAI call PER MODULE from the same graph+docs context
-- gap analysis uses (see server/llm_test_generation.py). One call covering an
-- entire large repo (hundreds of modules) hit gpt-4o-mini's own output-token
-- ceiling long before every module got even one case; scoping each call to a
-- single module's real surface fixes that. Replaced wholesale per (repo,
-- module) pair on each generation pass for that module, same "fresh pass
-- supersedes the last one" rule as gap findings -- other modules' previously
-- generated cases are untouched.
CREATE TABLE IF NOT EXISTS test_cases (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    module TEXT,                      -- which module (domain) this case was generated for; NULL on
                                       -- rows from before per-module generation existed (legacy, harmless)
    run_id TEXT,                      -- which run's graph was used to generate these
    title TEXT NOT NULL,
    category TEXT NOT NULL,           -- 'happy_path' | 'edge_case' | 'error_handling'
    target TEXT,                      -- the function/class/flow this case exercises, if identifiable
    preconditions TEXT,
    steps TEXT NOT NULL,              -- JSON-encoded list of ordered step strings
    expected_result TEXT NOT NULL,    -- overall outcome: for a functional case, the last step's expected result
    edge_case_description TEXT,       -- legacy (unit-style cases); functional cases carry expected results per step
    description TEXT,                 -- what the case verifies and why (ADO's summary/description)
    priority INTEGER,                 -- 1 (critical path) .. 4 (rare); NULL on legacy cases
    covers TEXT,                      -- JSON list of real code names (functions/classes/Class.method) the scenario exercises
    created_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

-- Friendly, business-English names for modules (see server/llm_module_naming.py)
-- -- purely cosmetic, keyed to the module's real dotted-path name (`domain`),
-- NOT to any one run's graph, so a friendly name survives "Run analysis"
-- being clicked again indefinitely -- unlike per-run enrichment (which the
-- graph itself carries and a fresh run wipes), this table is the one piece
-- of module-level data in this app that's deliberately NOT tied to a run_id.
CREATE TABLE IF NOT EXISTS module_labels (
    repo_id TEXT NOT NULL,
    module TEXT NOT NULL,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, module),
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_REPO_COLUMN_MIGRATIONS = {
    "github_owner": "TEXT",
    "github_repo": "TEXT",
    "github_branch": "TEXT DEFAULT 'main'",
    "github_pat_enc": "TEXT",
}

_TEST_CASES_COLUMN_MIGRATIONS = {
    "module": "TEXT",  # added when test-case generation moved from repo-wide to per-module
    # added when test cases became functional / ADO-shaped (steps are now {action, expected} objects,
    # stored in the same JSON `steps` column -- legacy rows still hold plain strings, and both render)
    "description": "TEXT",
    "priority": "INTEGER",
    "covers": "TEXT",
}


def _migrate_columns(conn: sqlite3.Connection, table: str, migrations: dict[str, str]) -> None:
    """Adds any columns introduced after a DB already existed -- CREATE TABLE
    IF NOT EXISTS only helps on a brand-new DB, so an existing production
    database needs an explicit ALTER TABLE per new column."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, col_type in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn, "repos", _REPO_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "test_cases", _TEST_CASES_COLUMN_MIGRATIONS)
        # The Playwright+BDD parser was retired -- any repo still configured
        # for it has no parser left to run; move it to the one supported
        # framework rather than leave it permanently broken.
        conn.execute("UPDATE repos SET framework = 'python_devcode' WHERE framework != 'python_devcode'")


# --------------------------------------------------------------------------- repos

def create_repo(payload: dict) -> dict:
    repo_id = str(uuid.uuid4())
    pat = payload.get("ado_pat") or ""
    github_pat = payload.get("github_pat") or ""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO repos
               (id, name, source_type, framework, local_path, ado_org, ado_project,
                ado_repo, ado_branch, ado_pat_enc,
                github_owner, github_repo, github_branch, github_pat_enc, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                repo_id,
                payload["name"],
                payload["source_type"],
                payload.get("framework", "python_devcode"),
                payload.get("local_path"),
                payload.get("ado_org"),
                payload.get("ado_project"),
                payload.get("ado_repo"),
                payload.get("ado_branch") or "main",
                crypto.encrypt(pat) if pat else None,
                payload.get("github_owner"),
                payload.get("github_repo"),
                payload.get("github_branch") or "main",
                crypto.encrypt(github_pat) if github_pat else None,
                now(),
            ),
        )
    return get_repo(repo_id)


def update_repo(repo_id: str, payload: dict) -> dict | None:
    existing = get_repo(repo_id, include_pat=True)
    if not existing:
        return None
    fields = [
        "name", "source_type", "framework", "local_path", "ado_org", "ado_project",
        "ado_repo", "ado_branch",
        "github_owner", "github_repo", "github_branch",
    ]
    updates = {f: payload[f] for f in fields if f in payload}
    if payload.get("ado_pat"):
        updates["ado_pat_enc"] = crypto.encrypt(payload["ado_pat"])
    if payload.get("github_pat"):
        updates["github_pat_enc"] = crypto.encrypt(payload["github_pat"])
    if not updates:
        return get_repo(repo_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        conn.execute(f"UPDATE repos SET {set_clause} WHERE id = ?", (*updates.values(), repo_id))
    return get_repo(repo_id)


def _row_to_repo(row: sqlite3.Row, include_pat: bool) -> dict:
    d = dict(row)
    enc = d.pop("ado_pat_enc", None)
    d["has_pat"] = bool(enc)
    if include_pat:
        d["ado_pat"] = crypto.decrypt(enc) if enc else ""
    github_enc = d.pop("github_pat_enc", None)
    d["has_github_pat"] = bool(github_enc)
    if include_pat:
        d["github_pat"] = crypto.decrypt(github_enc) if github_enc else ""
    # Leftover columns from the retired BDD/ADO-Test-Case pipeline -- still
    # present on an existing production DB (SQLite doesn't drop columns via
    # CREATE TABLE IF NOT EXISTS), but no longer meaningful.
    d.pop("fetch_test_cases", None)
    d.pop("ado_area_path", None)
    return d


def get_repo(repo_id: str, include_pat: bool = False) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return _row_to_repo(row, include_pat) if row else None


def list_repos() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
    return [_row_to_repo(r, include_pat=False) for r in rows]


def delete_repo(repo_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM doc_gap_findings WHERE document_id IN (SELECT id FROM documents WHERE repo_id = ?)",
            (repo_id,),
        )
        conn.execute("DELETE FROM repo_gap_findings WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM test_cases WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM module_labels WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM documents WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM runs WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))


# --------------------------------------------------------------------------- runs

def create_run(repo_id: str) -> dict:
    run_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO runs (id, repo_id, started_at, status) VALUES (?,?,?,?)",
            (run_id, repo_id, now(), "running"),
        )
    return get_run(run_id)


def finish_run(run_id: str, *, status: str, stats: dict | None = None,
                findings: list | None = None, graph_path: str | None = None,
                html_path: str | None = None, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE runs SET finished_at=?, status=?, stats_json=?, findings_json=?,
               graph_path=?, html_path=?, error=? WHERE id=?""",
            (
                now(),
                status,
                json.dumps(stats) if stats is not None else None,
                json.dumps(findings) if findings is not None else None,
                graph_path,
                html_path,
                error,
                run_id,
            ),
        )


def update_run_stats(run_id: str, stats: dict) -> None:
    """Patches just stats_json, leaving status/findings/timestamps untouched --
    used by the enrichment endpoint, which annotates an already-finished run
    rather than re-running analysis."""
    with get_conn() as conn:
        conn.execute("UPDATE runs SET stats_json=? WHERE id=?", (json.dumps(stats), run_id))


def get_run(run_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["stats"] = json.loads(d.pop("stats_json")) if d.get("stats_json") else None
    d["findings"] = json.loads(d.pop("findings_json")) if d.get("findings_json") else None
    return d


def list_runs(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE repo_id = ? ORDER BY started_at DESC", (repo_id,)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        stats = json.loads(d.pop("stats_json")) if d.get("stats_json") else None
        findings = json.loads(d.pop("findings_json")) if d.get("findings_json") else []
        d["stats"] = stats
        d["finding_count"] = len(findings) if findings else 0
        out.append(d)
    return out


def get_latest_successful_run(repo_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE repo_id = ? AND status = 'success' ORDER BY started_at DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["stats"] = json.loads(d.pop("stats_json")) if d.get("stats_json") else None
    d["findings"] = json.loads(d.pop("findings_json")) if d.get("findings_json") else None
    return d


# --------------------------------------------------------------------------- documents

def create_document(repo_id: str, payload: dict) -> dict:
    doc_id = str(uuid.uuid4())
    ts = now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (id, repo_id, name, content, domain, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (doc_id, repo_id, payload["name"], payload["content"], payload.get("domain"), ts, ts),
        )
    return get_document(doc_id)


def update_document(doc_id: str, payload: dict) -> dict | None:
    if not get_document(doc_id):
        return None
    fields = ["name", "content", "domain"]
    updates = {f: payload[f] for f in fields if f in payload}
    if not updates:
        return get_document(doc_id)
    updates["updated_at"] = now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        conn.execute(f"UPDATE documents SET {set_clause} WHERE id = ?", (*updates.values(), doc_id))
    return get_document(doc_id)


def get_document(doc_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE repo_id = ? ORDER BY created_at DESC", (repo_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_document(doc_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM doc_gap_findings WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# --------------------------------------------------------------------------- repo gap findings

def replace_repo_gap_findings(
    repo_id: str, run_id: str | None, source_docs: list[str], findings: list[dict]
) -> None:
    """Idempotent: drops this repo's previous gap findings and stores the new set --
    a fresh comparison pass (over ALL of the repo's documents combined) supersedes
    the last one rather than accumulating stale results."""
    ts = now()
    joined_docs = ", ".join(source_docs)
    with get_conn() as conn:
        conn.execute("DELETE FROM repo_gap_findings WHERE repo_id = ?", (repo_id,))
        for f in findings:
            conn.execute(
                "INSERT INTO repo_gap_findings (id, repo_id, run_id, category, description, source_docs, created_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), repo_id, run_id, f["category"], f["description"], joined_docs, ts),
            )


def list_repo_gap_findings(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM repo_gap_findings WHERE repo_id = ? ORDER BY created_at DESC", (repo_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- test cases

def replace_test_cases(repo_id: str, module: str, run_id: str | None, cases: list[dict]) -> None:
    """Idempotent per (repo, module): drops just that module's previously
    generated test cases and stores the new set -- a fresh generation pass
    for a module supersedes its last one, but leaves every other module's
    cases untouched (generation is scoped per module precisely so a large
    repo's modules can be generated one at a time, not wiped by each other)."""
    ts = now()
    with get_conn() as conn:
        conn.execute("DELETE FROM test_cases WHERE repo_id = ? AND module = ?", (repo_id, module))
        for c in cases:
            conn.execute(
                """INSERT INTO test_cases
                   (id, repo_id, module, run_id, title, category, target, preconditions, steps, expected_result,
                    edge_case_description, description, priority, covers, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), repo_id, module, run_id, c["title"], c["category"], c.get("target"),
                    c.get("preconditions"), json.dumps(c["steps"]), c["expected_result"],
                    c.get("edge_case_description"), c.get("description"), c.get("priority"),
                    json.dumps(c.get("covers") or []), ts,
                ),
            )


def list_test_cases(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            # newest generation pass first; within a pass, most important first (legacy cases have no priority -> last)
            "SELECT * FROM test_cases WHERE repo_id = ? ORDER BY created_at DESC, COALESCE(priority, 5), rowid",
            (repo_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["steps"] = json.loads(d["steps"]) if d["steps"] else []
        d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
        out.append(d)
    return out


# --------------------------------------------------------------------------- module labels

def set_module_labels(repo_id: str, labels: dict[str, str]) -> None:
    """Upserts a friendly display name for each (repo_id, module) pair given.
    Independent of any run -- see module_labels' schema comment for why."""
    ts = now()
    with get_conn() as conn:
        for module, display_name in labels.items():
            conn.execute(
                """INSERT INTO module_labels (repo_id, module, display_name, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(repo_id, module) DO UPDATE SET display_name = excluded.display_name,
                                                               updated_at = excluded.updated_at""",
                (repo_id, module, display_name, ts),
            )


def get_module_labels(repo_id: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module, display_name FROM module_labels WHERE repo_id = ?", (repo_id,)
        ).fetchall()
    return {r["module"]: r["display_name"] for r in rows}
