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
    source_type TEXT NOT NULL,        -- 'local' | 'ado_git' | 'github_git'
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


def _migrate_repo_columns(conn: sqlite3.Connection) -> None:
    """Adds any repos columns introduced after a DB already existed --
    CREATE TABLE IF NOT EXISTS only helps on a brand-new DB, so an existing
    production database needs an explicit ALTER TABLE per new column."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(repos)").fetchall()}
    for col, col_type in _REPO_COLUMN_MIGRATIONS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {col_type}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_repo_columns(conn)
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
