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
    kind TEXT,                        -- 'feature' (a user can see/use it) | 'supporting' (behind the scenes); NULL on older rows = feature
    description TEXT,                 -- one plain-English sentence for a non-technical reader
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, module),
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

-- Atomic acceptance-criteria requirements extracted from ONE user-story
-- document and judged against the whole codebase -- "Story Coverage" (see
-- server/llm_story_coverage.py). One combined LLM call per (repo, document)
-- pass does decomposition AND judgment together -- the same "one prompt in,
-- one JSON array out" shape gap analysis and test generation already use.
-- `source` distinguishes a row this LLM/manual pass just wrote ('document')
-- from a future human-confirmed override ('human_confirmed', not written by
-- anything today) -- replace_story_requirements only wipes 'document' rows
-- for the (repo, doc) pair being refreshed, so a confirmed answer (once that
-- concept exists) survives a rerun, the same way test_cases' per-module
-- scoping already protects other modules' cases today.
CREATE TABLE IF NOT EXISTS story_requirements (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    doc_id TEXT,                              -- NULL for a journey-derived row (source='derived'): no document, no NOT NULL fake value
    run_id TEXT,                              -- which run's graph was used as the comparison baseline
    text TEXT NOT NULL,                       -- the atomic acceptance-criterion claim
    type TEXT NOT NULL,                       -- 'positive' | 'negative'
    status TEXT NOT NULL,                     -- 'supported' | 'contradicted' | 'unknown'
    rationale TEXT,                           -- one-sentence reason for the judgment
    covers TEXT,                              -- JSON list of real code names (functions/classes/Class.method)
    source TEXT NOT NULL DEFAULT 'document',  -- 'document' | 'derived' | 'human_confirmed'
    created_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id),
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);

-- A "journey" -- an end-to-end user flow traced from a real HTTP entry point
-- through the call graph, clustered by shared route prefix (see
-- kg/journeys.py). `id` is a STABLE slug of the route prefix, not a fresh
-- uuid, and a fresh detection pass UPSERTS by that key (server/db.py's
-- replace_journeys) rather than delete-then-reinsert, so a story_requirements
-- row pointing at one (especially a human-confirmed answer) survives a
-- repo being re-analyzed and journeys being re-detected.
CREATE TABLE IF NOT EXISTS journeys (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    run_id TEXT,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,              -- the route prefix this journey clustered around, e.g. "/checkout"
    entry_point_ids TEXT NOT NULL,     -- JSON list of graph node ids
    node_ids TEXT NOT NULL,            -- JSON list of graph node ids reachable from those entry points
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

-- A long-running, many-document operation (bulk Story Coverage or bulk test
-- generation across every document in a repo) runs on a background thread,
-- not inline in the request -- see server/app.py's run-all routes. This is
-- that thread's progress, polled by the frontend (the first such pattern in
-- this codebase; nothing before this ran long enough to need one). `kind`
-- distinguishes which operation this is, since both share one shape.
CREATE TABLE IF NOT EXISTS bulk_jobs (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    kind TEXT NOT NULL,                -- 'story_coverage' | 'test_generation' | 'test_verification'
    status TEXT NOT NULL,              -- 'running' | 'success' | 'failed'
    total INTEGER NOT NULL,            -- documents to process
    processed INTEGER NOT NULL DEFAULT 0,
    current_document_name TEXT,
    results_json TEXT,                 -- JSON list of {document_id, document_name, ok, detail}, appended to as each document finishes
    error TEXT,                        -- set only on a failure that aborted the whole job, not a single document's
    started_at TEXT NOT NULL,
    finished_at TEXT,
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
    # WAL + a generous busy_timeout: bulk_jobs' background thread (one writer,
    # looping documents) runs alongside the request thread polling its
    # progress -- the first real concurrent DB access in this codebase. The
    # default rollback-journal mode serializes writers hard enough to risk
    # "database is locked" under that; WAL lets readers (a poll) proceed
    # without blocking on a writer, and the busy_timeout covers the rest.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
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

_MODULE_LABELS_COLUMN_MIGRATIONS = {
    "kind": "TEXT",         # added when names became screen/feature-oriented for a non-technical audience
    "description": "TEXT",
}

_TEST_CASES_COLUMN_MIGRATIONS = {
    "module": "TEXT",  # added when test-case generation moved from repo-wide to per-module
    # added when test cases became functional / ADO-shaped (steps are now {action, expected} objects,
    # stored in the same JSON `steps` column -- legacy rows still hold plain strings, and both render)
    "description": "TEXT",
    "priority": "INTEGER",
    "covers": "TEXT",
    # added for the verification pass (server/llm_test_verification.py) -- NULL means
    # "never verified", not "unknown"; regenerating a module resets both to NULL since
    # the old judgment was about code that may no longer match the new cases.
    "status": "TEXT",
    "rationale": "TEXT",
    # NULL for an ordinary code-generated case; set when a case was generated
    # FROM a story requirement instead (server/llm_requirement_test_generation.py)
    # -- that case's status/rationale/covers are inherited from the requirement,
    # never re-derived, so this just records which requirement it came from.
    "source_requirement_id": "TEXT",
    # NULL for an ordinary case; set when a case was generated from a whole
    # JOURNEY instead of one requirement (server/llm_journey_test_generation.py)
    # -- mutually exclusive with source_requirement_id in practice, but kept as
    # a separate column rather than overloading one, since a journey-sourced
    # case's steps span several requirements, not one.
    "source_journey_id": "TEXT",
    # NULL for an ordinary case; set when a case was generated directly from a
    # DOCUMENT's own story text (server/llm_requirement_test_generation.py's
    # ungrounded generators) -- independent of source_requirement_id (a
    # per-requirement-generated case gets BOTH set: it belongs to one
    # requirement AND to that requirement's document). This is what the
    # document-scoped Verify pass groups by, since these cases start with
    # status=NULL, covers=[], module=NULL (no code was ever shown at
    # generation time) and need a way to be found again afterward.
    "source_doc_id": "TEXT",
}

_STORY_REQUIREMENTS_COLUMN_MIGRATIONS = {
    # Which journey a DERIVED requirement (source='derived') was found inside
    # -- NULL for a document-sourced or not-yet-journey-linked row. A
    # document-sourced requirement's journey membership is computed at read
    # time from citation overlap (kg/source_lookup.py's
    # resolve_citation_node_id against journeys.node_ids), never stored here.
    "journey_id": "TEXT",
    # The yes/no question to show a human, only set when status='needs_human_input'
    # (server/llm_journey_requirements.py) -- NULL for every other row.
    "question": "TEXT",
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
        _migrate_columns(conn, "module_labels", _MODULE_LABELS_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "story_requirements", _STORY_REQUIREMENTS_COLUMN_MIGRATIONS)
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
        conn.execute("DELETE FROM story_requirements WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM journeys WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM bulk_jobs WHERE repo_id = ?", (repo_id,))
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
        conn.execute("DELETE FROM story_requirements WHERE doc_id = ?", (doc_id,))
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
    CODE-generated test cases and stores the new set -- a fresh generation
    pass for a module supersedes its last one, but leaves every other
    module's cases untouched (generation is scoped per module precisely so a
    large repo's modules can be generated one at a time, not wiped by each
    other).

    Scoped to source_requirement_id/source_doc_id/source_journey_id all NULL
    -- i.e. ONLY rows this same function created -- not just `module`, even
    though every row this function has ever inserted only ever had `module`
    to go on. A story- or journey-driven case starts with module=NULL and
    only gets a real module assigned once a later Verify pass resolves one
    (server/app.py's document-scoped Verify) -- once that happens, it can
    legitimately share a module name with this module's own code-generated
    batch. Scoping the delete by module alone would silently wipe those
    verified cases out from under their own feature every time this module
    gets regenerated, even though "Regenerate" only ever meant "refresh my
    own code-driven batch," not "clear everything under this module name"."""
    ts = now()
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM test_cases WHERE repo_id = ? AND module = ?
               AND source_requirement_id IS NULL AND source_doc_id IS NULL AND source_journey_id IS NULL""",
            (repo_id, module),
        )
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


def delete_test_cases_by_source_doc(repo_id: str, doc_id: str) -> None:
    """Opt-in cleanup for a document-level (re)generation pass -- "Replace
    existing test cases for this document" in the UI. Scoped by
    source_doc_id, never by module (a document's cases can legitimately span
    several, or none yet), and never touches a case from a different
    document or from module-level code-driven generation."""
    with get_conn() as conn:
        conn.execute("DELETE FROM test_cases WHERE repo_id = ? AND source_doc_id = ?", (repo_id, doc_id))


def delete_test_cases_by_module(repo_id: str, module: str) -> int:
    """Manual "Clear" for one module bucket in the Test Cases tab -- deletes
    every case whose `module` equals the given string, regardless of origin
    (code/story/journey) or whether that module still exists in the latest
    analysis run's graph. The latter is the actual point: a module can be
    renamed or restructured between runs (e.g. a repo's real modules changing
    from a single "code" lump to "inventory"/"notifications"/"pricing"),
    leaving old cases permanently orphaned under a module name nothing
    current can regenerate or filter meaningfully by -- this is the only way
    to clear those out. Returns how many rows were deleted."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM test_cases WHERE repo_id = ? AND module = ?", (repo_id, module))
        return cur.rowcount


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


def create_test_case(
    repo_id: str, module: str | None, run_id: str | None, case: dict,
    source_requirement_id: str | None = None, source_journey_id: str | None = None,
    source_doc_id: str | None = None,
) -> dict:
    """A single new row, never touching any other test case -- used for a
    requirement-sourced case (server/llm_requirement_test_generation.py) or a
    journey-sourced one (server/llm_journey_test_generation.py), where
    replace_test_cases' module-scoped wipe would be wrong: it would delete
    every other case already generated for that module. `module` is None for
    an ungrounded, not-yet-verified story-driven case (see
    update_test_case_verification, which resolves it later)."""
    ts = now()
    case_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO test_cases
               (id, repo_id, module, run_id, title, category, target, preconditions, steps, expected_result,
                edge_case_description, description, priority, covers, status, rationale,
                source_requirement_id, source_journey_id, source_doc_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id, repo_id, module, run_id, case["title"], case["category"], case.get("target"),
                case.get("preconditions"), json.dumps(case["steps"]), case["expected_result"],
                None, case.get("description"), case.get("priority"), json.dumps(case.get("covers") or []),
                case.get("status"), case.get("rationale"), source_requirement_id, source_journey_id, source_doc_id, ts,
            ),
        )
    return {
        **case, "id": case_id, "repo_id": repo_id, "module": module,
        "source_requirement_id": source_requirement_id, "source_journey_id": source_journey_id,
        "source_doc_id": source_doc_id,
    }


def update_test_case_verification(
    case_id: str, status: str, rationale: str | None, covers: list[str], module: str | None,
) -> None:
    """Document-scoped Verify's patch for a story-driven case -- it starts
    with covers=[] and module=NULL (no code was ever shown at generation
    time), so this patches all four fields at once, resolving `module` for
    the first time. Always overwrites covers/module with the fresh result --
    re-running Verify is idempotent. (A code-driven case's status/rationale
    are never stored this way -- they're derived fresh at read time from
    Story Coverage's own judgments, see server/llm_story_coverage.py's
    link_requirements_to_cases.)"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE test_cases SET status = ?, rationale = ?, covers = ?, module = ? WHERE id = ?",
            (status, rationale, json.dumps(covers or []), module, case_id),
        )


# --------------------------------------------------------------------------- module labels

def set_module_labels(repo_id: str, labels: dict) -> None:
    """Upserts a friendly label for each (repo_id, module) pair given. A value is
    either a plain name (str -- the original form) or {name, kind, description}.
    Independent of any run -- see module_labels' schema comment for why."""
    ts = now()
    with get_conn() as conn:
        for module, value in labels.items():
            info = {"name": value} if isinstance(value, str) else value
            conn.execute(
                """INSERT INTO module_labels (repo_id, module, display_name, kind, description, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(repo_id, module) DO UPDATE SET display_name = excluded.display_name,
                                                               kind = excluded.kind,
                                                               description = excluded.description,
                                                               updated_at = excluded.updated_at""",
                (repo_id, module, info["name"], info.get("kind"), info.get("description"), ts),
            )


def get_module_labels(repo_id: str) -> dict[str, str]:
    """{module: display name} -- what most callers want."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module, display_name FROM module_labels WHERE repo_id = ?", (repo_id,)
        ).fetchall()
    return {r["module"]: r["display_name"] for r in rows}


def get_module_label_details(repo_id: str) -> dict[str, dict]:
    """{module: {name, kind, description}}; older rows have no kind, which means a feature."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module, display_name, kind, description FROM module_labels WHERE repo_id = ?", (repo_id,)
        ).fetchall()
    return {
        r["module"]: {"name": r["display_name"], "kind": r["kind"] or "feature", "description": r["description"]}
        for r in rows
    }


# --------------------------------------------------------------------------- story coverage requirements

def replace_story_requirements(repo_id: str, doc_id: str, run_id: str | None, requirements: list[dict]) -> None:
    """Idempotent per (repo, document): drops just this document's previously
    extracted/judged requirements written by an LLM or manual pass
    (source='document') and stores the new set. Never touches a future
    human-confirmed row for this same document, and never touches any other
    document's requirements -- full wholesale replace (like gap findings)
    would be wrong here, since a human-confirmed answer must be able to
    survive a rerun."""
    ts = now()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM story_requirements WHERE repo_id = ? AND doc_id = ? AND source = 'document'",
            (repo_id, doc_id),
        )
        for r in requirements:
            conn.execute(
                """INSERT INTO story_requirements
                   (id, repo_id, doc_id, run_id, text, type, status, rationale, covers, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), repo_id, doc_id, run_id, r["text"], r["type"], r["status"],
                    r.get("rationale"), json.dumps(r.get("covers") or []), "document", ts,
                ),
            )


def list_story_requirements(repo_id: str, doc_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if doc_id:
            rows = conn.execute(
                "SELECT * FROM story_requirements WHERE repo_id = ? AND doc_id = ? ORDER BY created_at DESC, rowid",
                (repo_id, doc_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM story_requirements WHERE repo_id = ? ORDER BY created_at DESC, rowid", (repo_id,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
        out.append(d)
    return out


def get_story_requirement(requirement_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM story_requirements WHERE id = ?", (requirement_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
    return d


def replace_derived_requirements(repo_id: str, journey_id: str, run_id: str | None, requirements: list[dict]) -> None:
    """Like replace_story_requirements, scoped to one journey's DERIVED rows
    only (`WHERE journey_id = ? AND source = 'derived'`) -- never touches a
    human-confirmed answer for this same journey (a prior derived row the
    human already resolved), another journey's rows, or any document-sourced
    requirement."""
    ts = now()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM story_requirements WHERE repo_id = ? AND journey_id = ? AND source = 'derived'",
            (repo_id, journey_id),
        )
        for r in requirements:
            conn.execute(
                """INSERT INTO story_requirements
                   (id, repo_id, doc_id, run_id, text, type, status, rationale, covers, source, journey_id, question, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), repo_id, None, run_id, r["text"], r["type"], r["status"],
                    r.get("rationale"), json.dumps(r.get("covers") or []), "derived", journey_id,
                    r.get("question"), ts,
                ),
            )


def confirm_story_requirement(requirement_id: str, status: str, rationale: str | None = None) -> dict | None:
    """A human answers a `needs_human_input` question: the row's status
    becomes whatever was confirmed and its source flips to 'human_confirmed'
    -- from this point it's indistinguishable from a document-sourced row
    everywhere else that reads story_requirements (rollup, citation, journey
    membership, test-case generation). This one field transition IS the
    "answers compound over time" mechanism -- nothing else needs to change."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE story_requirements SET status = ?, rationale = COALESCE(?, rationale), source = 'human_confirmed' WHERE id = ?",
            (status, rationale, requirement_id),
        )
    return get_story_requirement(requirement_id)


# --------------------------------------------------------------------------- journeys

def replace_journeys(repo_id: str, run_id: str | None, journeys: list[dict]) -> None:
    """UPSERTS by journey id (a stable slug of its route prefix, see
    kg/journeys.py) -- never delete-then-reinsert. A fresh detection pass
    must not orphan a story_requirements.journey_id reference, especially a
    human-confirmed one, so a journey whose route prefix no longer has any
    entry points is simply left in place rather than removed."""
    ts = now()
    with get_conn() as conn:
        for j in journeys:
            existing = conn.execute("SELECT id FROM journeys WHERE id = ?", (j["id"],)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE journeys SET run_id=?, name=?, prefix=?, entry_point_ids=?, node_ids=?, updated_at=? WHERE id=?",
                    (run_id, j["name"], j["prefix"], json.dumps(j["entry_point_ids"]), json.dumps(j["node_ids"]), ts, j["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO journeys (id, repo_id, run_id, name, prefix, entry_point_ids, node_ids, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (j["id"], repo_id, run_id, j["name"], j["prefix"], json.dumps(j["entry_point_ids"]), json.dumps(j["node_ids"]), ts, ts),
                )


def list_journeys(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM journeys WHERE repo_id = ? ORDER BY prefix", (repo_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["entry_point_ids"] = json.loads(d["entry_point_ids"]) if d.get("entry_point_ids") else []
        d["node_ids"] = json.loads(d["node_ids"]) if d.get("node_ids") else []
        out.append(d)
    return out


def get_journey(journey_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM journeys WHERE id = ?", (journey_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["entry_point_ids"] = json.loads(d["entry_point_ids"]) if d.get("entry_point_ids") else []
    d["node_ids"] = json.loads(d["node_ids"]) if d.get("node_ids") else []
    return d


# --------------------------------------------------------------------------- bulk jobs

def create_bulk_job(repo_id: str, kind: str, total: int) -> dict:
    job_id = str(uuid.uuid4())
    ts = now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_jobs (id, repo_id, kind, status, total, processed, results_json, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (job_id, repo_id, kind, "running", total, 0, json.dumps([]), ts),
        )
    return get_bulk_job(job_id)


def update_bulk_job_progress(job_id: str, current_document_name: str, result_entry: dict) -> None:
    """Called once per document as a bulk job works through them -- bumps
    `processed` and appends one outcome to `results_json`, read-modify-write
    under this same connection's transaction (one writer thread calls this
    serially, never concurrently with itself, so no extra locking needed
    beyond what get_conn()'s WAL/busy_timeout already provides against the
    request thread polling get_bulk_job concurrently)."""
    with get_conn() as conn:
        row = conn.execute("SELECT results_json, processed FROM bulk_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        results = json.loads(row["results_json"]) if row["results_json"] else []
        results.append(result_entry)
        conn.execute(
            "UPDATE bulk_jobs SET processed = ?, current_document_name = ?, results_json = ? WHERE id = ?",
            (row["processed"] + 1, current_document_name, json.dumps(results), job_id),
        )


def finish_bulk_job(job_id: str, status: str, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bulk_jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, now(), job_id),
        )


def get_bulk_job(job_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bulk_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["results"] = json.loads(d["results_json"]) if d.get("results_json") else []
    return d
