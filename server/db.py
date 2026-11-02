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
CREATE TABLE IF NOT EXISTS requirement_assessments (
    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_case_assessments (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    run_id TEXT,
    model TEXT,
    snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS test_case_assessments_case ON test_case_assessments(repo_id, case_id);

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
CREATE TABLE IF NOT EXISTS repo_gap_assessments (
    repo_id TEXT PRIMARY KEY REFERENCES repos(id),
    run_id TEXT,
    assessed_at TEXT NOT NULL
);
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

-- One row per (repo, day) -- a daily snapshot of the traceability dashboard's own
-- summary numbers (see server/traceability_dashboard.py), so the dashboard can show
-- a trend line without a much bigger per-item history design. Written opportunistically
-- (upserted) whenever the dashboard is fetched on a day that doesn't have a row yet --
-- there's no scheduler in this app, so "whoever looks at the dashboard that day" is
-- what advances the trend, same as this app's other read-triggered computations.
-- `summary_json` is the same summary dict the live dashboard renders, the same
-- JSON-blob-column precedent `runs.stats_json` already established.
CREATE TABLE IF NOT EXISTS dashboard_snapshots (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    date TEXT NOT NULL,                -- "YYYY-MM-DD", one row per repo per day
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_snapshots_repo_date ON dashboard_snapshots(repo_id, date);

-- A human's own judgment on an AI assessment (test case or story requirement),
-- kept as its own durable, timestamped row rather than overwriting the AI's
-- status in place -- a reviewer disagreeing with the AI is itself a fact worth
-- keeping, not something that should erase what the AI originally said.
-- server/evidence_rules.py reads these to compute each item's evidence_level,
-- and the review-accuracy endpoint reads them to measure the AI against.
CREATE TABLE IF NOT EXISTS human_reviews (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    target_type TEXT NOT NULL,         -- 'test_case' | 'story_requirement'
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,            -- 'confirmed_supported' | 'confirmed_contradicted' | 'rejected_ai_judgment' | 'needs_more_info'
    note TEXT,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);
CREATE INDEX IF NOT EXISTS idx_human_reviews_target ON human_reviews(target_type, target_id);
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

_HUMAN_REVIEWS_COLUMN_MIGRATIONS = {
    # evidence_rules.evidence_level(target) AS IT WAS the instant before this
    # review was recorded -- a SNAPSHOT, never re-derived later. Critical: for
    # a story_requirement, confirm_story_requirement overwrites status right
    # after this row is written, so re-deriving evidence_level from the LIVE
    # row at review-accuracy time would compare the human's decision against
    # the human's own confirmed answer, not the AI's original judgment --
    # turning every correction into apparent agreement. NULL on a row written
    # before this column existed.
    "ai_level_at_review": "TEXT",
    "assessment_version": "TEXT",
    "assessment_snapshot": "TEXT",
}

_RUNS_COLUMN_MIGRATIONS = {
    # Best-effort `git rev-parse HEAD` at analysis time (server/runner.py) --
    # NULL for a repo that isn't a git checkout (e.g. source_type='upload').
    # Audit trail only: staleness detection (server/evidence_rules.py) compares
    # run_id, not commit_sha, since a reliable SHA isn't available for every repo.
    "commit_sha": "TEXT",
}

_MODULE_LABELS_COLUMN_MIGRATIONS = {
    "kind": "TEXT",         # added when names became screen/feature-oriented for a non-technical audience
    "description": "TEXT",
}

_TEST_CASES_COLUMN_MIGRATIONS = {
    "generation_context": "TEXT",
    "reassessment": "TEXT",
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
    # 'generated' (default, every existing row) or 'imported' (server/test_case_import.py,
    # for a case brought in from an already-existing suite, e.g. a QA team's real ADO
    # export). Orthogonal to the source_* columns above, which are about which
    # GENERATION mechanism produced a case -- an imported case has none of those set,
    # same as an ordinary code-driven one, so this is the only way to tell them apart.
    "created_via": "TEXT DEFAULT 'generated'",
    # When this case's status/covers were last written by an LLM pass, which
    # model did it, and which real file each covers[] name resolved to
    # (kg.source_lookup.find_citation_path, best-effort -- a name that doesn't
    # resolve gets `null` in the list rather than being dropped). NULL/NULL/NULL
    # means "never verified" -- same meaning status=NULL already carries.
    "assessed_at": "TEXT",
    "assessed_model": "TEXT",
    "cited_paths": "TEXT",
}

_DOCUMENTS_COLUMN_MIGRATIONS = {
    # 'manual' (default) or 'imported' (server/work_item_import.py, from a bulk
    # work-item export). Existing rows default to 'manual' even where some were
    # historically imported before this column existed -- that's an unavoidable
    # data-quality gap on upgrade, not something worth trying to backfill.
    "created_via": "TEXT DEFAULT 'manual'",
}

_STORY_REQUIREMENTS_COLUMN_MIGRATIONS = {
    "source_context": "TEXT",
    "document_fingerprint": "TEXT",
    "retired": "INTEGER NOT NULL DEFAULT 0",
    # Which journey a DERIVED requirement (source='derived') was found inside
    # -- NULL for a document-sourced or not-yet-journey-linked row. A
    # document-sourced requirement's journey membership is computed at read
    # time from citation overlap (kg/source_lookup.py's
    # resolve_citation_node_id against journeys.node_ids), never stored here.
    "journey_id": "TEXT",
    # The yes/no question to show a human, only set when status='needs_human_input'
    # (server/llm_journey_requirements.py) -- NULL for every other row.
    "question": "TEXT",
    # Same provenance fields as _TEST_CASES_COLUMN_MIGRATIONS above, same reasoning.
    "assessed_at": "TEXT",
    "assessed_model": "TEXT",
    "cited_paths": "TEXT",
    # The AI's LAST judgment, frozen the instant it was written by an AI pass
    # (replace_story_requirements / replace_derived_requirements) and NEVER
    # touched again afterward -- not even by confirm_story_requirement, which
    # keeps overwriting status/rationale/covers in place every time a human
    # answers. Without a frozen copy, a SECOND confirmation of the same
    # requirement would read the FIRST human's answer back out of status/covers
    # and mistake it for a fresh AI judgment -- this is what review-accuracy
    # actually compares a human decision against (server/app.py's
    # api_confirm_story_requirement), never the live, human-mutable columns.
    "ai_status": "TEXT",
    "ai_covers": "TEXT",
    "ai_rationale": "TEXT",
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
        _migrate_columns(conn, "runs", _RUNS_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "test_cases", _TEST_CASES_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "documents", _DOCUMENTS_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "repo_gap_findings", {"source_context":"TEXT"})
        _migrate_columns(conn, "module_labels", _MODULE_LABELS_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "story_requirements", _STORY_REQUIREMENTS_COLUMN_MIGRATIONS)
        _migrate_columns(conn, "human_reviews", _HUMAN_REVIEWS_COLUMN_MIGRATIONS)
        _migrate_columns(conn,"bulk_jobs",{"owner_pid":"INTEGER"})
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
        conn.execute("DELETE FROM repo_gap_assessments WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM dashboard_snapshots WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM requirement_assessments WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM test_case_assessments WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM test_cases WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM module_labels WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM story_requirements WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM journeys WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM bulk_jobs WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM human_reviews WHERE repo_id = ?", (repo_id,))
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
                html_path: str | None = None, error: str | None = None,
                commit_sha: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE runs SET finished_at=?, status=?, stats_json=?, findings_json=?,
               graph_path=?, html_path=?, error=?, commit_sha=? WHERE id=?""",
            (
                now(),
                status,
                json.dumps(stats) if stats is not None else None,
                json.dumps(findings) if findings is not None else None,
                graph_path,
                html_path,
                error,
                commit_sha,
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
            """INSERT INTO documents (id, repo_id, name, content, domain, created_via, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (doc_id, repo_id, payload["name"], payload["content"], payload.get("domain"),
             payload.get("created_via", "manual"), ts, ts),
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
        conn.execute("INSERT OR REPLACE INTO repo_gap_assessments (repo_id, run_id, assessed_at) VALUES (?,?,?)", (repo_id, run_id, ts))
        for f in findings:
            conn.execute(
                "INSERT INTO repo_gap_findings (id, repo_id, run_id, category, description, source_docs, created_at, source_context) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), repo_id, run_id, f["category"], f["description"], joined_docs, ts, json.dumps(f.get("source_context")) if f.get("source_context") else None),
            )


def list_repo_gap_findings(repo_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM repo_gap_findings WHERE repo_id = ? ORDER BY created_at DESC", (repo_id,)
        ).fetchall()
    return [{**dict(r), "source_context":json.loads(r["source_context"]) if r["source_context"] else None} for r in rows]


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
               AND source_requirement_id IS NULL AND source_doc_id IS NULL AND source_journey_id IS NULL
               AND COALESCE(created_via, 'generated') != 'imported'""",
            (repo_id, module),
        )
        for c in cases:
            conn.execute(
                """INSERT INTO test_cases
                   (id, repo_id, module, run_id, title, category, target, preconditions, steps, expected_result,
                    edge_case_description, description, priority, covers, created_at, generation_context)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), repo_id, module, run_id, c["title"], c["category"], c.get("target"),
                    c.get("preconditions"), json.dumps(c["steps"]), c["expected_result"],
                    c.get("edge_case_description"), c.get("description"), c.get("priority"),
                    json.dumps(c.get("covers") or []), ts, json.dumps(c.get("generation_context")) if c.get("generation_context") else None,
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
        d["generation_context"] = json.loads(d["generation_context"]) if d.get("generation_context") else None
        d["reassessment"] = json.loads(d["reassessment"]) if d.get("reassessment") else None
        d["steps"] = json.loads(d["steps"]) if d["steps"] else []
        d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
        d["cited_paths"] = json.loads(d["cited_paths"]) if d.get("cited_paths") else []
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


def import_test_cases(repo_id: str, cases: list[dict]) -> list[dict]:
    """Bulk-inserts externally-imported test cases (server/test_case_import.py).
    ADDITIVE, never a delete -- unlike replace_test_cases' module-scoped
    delete-then-insert for a fresh code-driven generation pass, an import is
    new data arriving alongside whatever already exists, never a
    regeneration of it. Each row starts exactly like an ungrounded,
    not-yet-verified story-driven case (module=NULL, covers=[], status=NULL
    -- see update_test_case_verification, which resolves these later), but
    with created_via='imported' and every source_* column NULL, since an
    import has no generation provenance at all."""
    ts = now()
    created = []
    with get_conn() as conn:
        for case in cases:
            case_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO test_cases
                   (id, repo_id, module, run_id, title, category, target, preconditions, steps, expected_result,
                    edge_case_description, description, priority, covers, created_via, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id, repo_id, None, None, case["title"], case["category"], case.get("target"),
                    case.get("preconditions"), json.dumps(case["steps"]), case["expected_result"],
                    None, case.get("description"), case.get("priority"), json.dumps(case.get("covers") or []),
                    "imported", ts,
                ),
            )
            created.append({**case, "id": case_id, "repo_id": repo_id, "module": None, "created_via": "imported"})
    return created


def update_test_case_verification(
    case_id: str, status: str, rationale: str | None, covers: list[str], module: str | None,
    assessed_model: str | None = None, cited_paths: list[str | None] | None = None, run_id: str | None = None,
    reassessment: dict | None = None, expected_case_fingerprint: str | None = None,
) -> None:
    """Save a fresh assessment transactionally, preserving the previous state in history.
    Only assessment metadata and resolved code linkage change. Test content, identity,
    origin, and human reviews are preserved. Reject concurrent content changes.
    """
    with get_conn() as conn:
        old = conn.execute("SELECT * FROM test_cases WHERE id = ?", (case_id,)).fetchone()
        if old is None:
            raise RuntimeError("Test case was deleted during assessment")
        old = dict(old)
        if expected_case_fingerprint:
            from .test_case_reassessment import case_fingerprint
            comparable = {**old, "steps": json.loads(old["steps"])}
            if case_fingerprint(comparable) != expected_case_fingerprint:
                raise RuntimeError("Test case changed during assessment; reassess its latest version")
        ts = now()
        snapshot = {"previous_case": old, "assessment": reassessment,
                    "status": status, "rationale": rationale, "covers": covers,
                    "cited_paths": cited_paths}
        conn.execute("INSERT INTO test_case_assessments VALUES (?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), old["repo_id"], case_id, ts, run_id, assessed_model, json.dumps(snapshot)))
        conn.execute(
            """UPDATE test_cases SET status = ?, rationale = ?, covers = ?, module = ?,
               assessed_at = ?, assessed_model = ?, cited_paths = ?, run_id = COALESCE(?, run_id),
               reassessment = ? WHERE id = ?""",
            (status, rationale, json.dumps(covers or []), module, ts, assessed_model,
             json.dumps(cited_paths) if cited_paths is not None else None, run_id,
             json.dumps(reassessment) if reassessment else None, case_id),
        )


def list_test_case_assessments(repo_id: str, case_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM test_case_assessments WHERE repo_id = ? AND case_id = ? ORDER BY rowid DESC",
                            (repo_id, case_id)).fetchall()
    return [{**dict(r), "snapshot": json.loads(r["snapshot"])} for r in rows]


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

def replace_story_requirements(repo_id, doc_id, run_id, requirements, assessed_model=None, expected_document_fingerprint=None):
    """Reconcile active criteria transactionally; retain IDs, old rows and reviews."""
    from .test_case_reassessment import documents_fingerprint
    ts=now()
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        document=conn.execute('SELECT * FROM documents WHERE id=? AND repo_id=?',(doc_id,repo_id)).fetchone()
        if document is None:
            raise RuntimeError('Document was deleted during assessment')
        doc_hash=documents_fingerprint([dict(document)])
        if expected_document_fingerprint and expected_document_fingerprint != doc_hash:
            raise RuntimeError('Document changed during assessment; assess the latest version')
        old=[dict(row) for row in conn.execute('SELECT * FROM story_requirements WHERE repo_id=? AND doc_id=?',(repo_id,doc_id))]
        key=lambda item:(' '.join(item['text'].split()),item['type'])
        used=set()
        for req in requirements:
            candidates=[row for row in old if key(row)==key(req) and row['id'] not in used]
            candidates.sort(key=lambda row:(row.get('retired',0),row['source']!='human_confirmed',row['created_at']))
            previous=candidates[0] if candidates else None
            req_id=previous['id'] if previous else str(uuid.uuid4())
            used.add(req_id)
            keep_human=bool(previous and previous['source']=='human_confirmed' and
                previous.get('document_fingerprint') in (None,doc_hash) and previous.get('run_id')==run_id and
                previous.get('source_context')==(json.dumps(req['source_context']) if req.get('source_context') else None))
            values=(repo_id,doc_id,run_id,req['text'],req['type'],
                previous['status'] if keep_human else req['status'],
                previous['rationale'] if keep_human else req.get('rationale'),
                previous['covers'] if keep_human else json.dumps(req.get('covers') or []),
                'human_confirmed' if keep_human else 'document',
                previous['created_at'] if previous else ts,ts,assessed_model,
                json.dumps(req['cited_paths']) if req.get('cited_paths') is not None else None,
                req['status'],json.dumps(req.get('covers') or []),req.get('rationale'),
                json.dumps(req['source_context']) if req.get('source_context') else None,doc_hash,req_id)
            if previous:
                conn.execute('INSERT INTO requirement_assessments VALUES (?,?,?,?,?)',
                    (str(uuid.uuid4()),repo_id,req_id,ts,json.dumps(previous)))
                conn.execute('''UPDATE story_requirements SET repo_id=?,doc_id=?,run_id=?,text=?,type=?,status=?,rationale=?,covers=?,source=?,
                    created_at=?,assessed_at=?,assessed_model=?,cited_paths=?,ai_status=?,ai_covers=?,ai_rationale=?,source_context=?,document_fingerprint=?,retired=0 WHERE id=?''',values)
            else:
                conn.execute('''INSERT INTO story_requirements
                    (repo_id,doc_id,run_id,text,type,status,rationale,covers,source,created_at,assessed_at,assessed_model,cited_paths,
                     ai_status,ai_covers,ai_rationale,source_context,document_fingerprint,id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',values)
        for row in old:
            if row['id'] not in used and (row['source']=='document' or any(key(row)==key(req) for req in requirements)):
                conn.execute('UPDATE story_requirements SET retired=1 WHERE id=?',(row['id'],))


def list_story_requirements(repo_id: str, doc_id: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if doc_id:
            rows = conn.execute(
                "SELECT * FROM story_requirements WHERE repo_id = ? AND doc_id = ? AND COALESCE(retired,0)=0 ORDER BY created_at DESC, rowid",
                (repo_id, doc_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM story_requirements WHERE repo_id = ? AND COALESCE(retired,0)=0 ORDER BY created_at DESC, rowid", (repo_id,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_context"] = json.loads(d["source_context"]) if d.get("source_context") else None
        d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
        d["cited_paths"] = json.loads(d["cited_paths"]) if d.get("cited_paths") else []
        d["ai_covers"] = json.loads(d["ai_covers"]) if d.get("ai_covers") else []
        out.append(d)
    return out


def get_story_requirement(requirement_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM story_requirements WHERE id = ?", (requirement_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["source_context"] = json.loads(d["source_context"]) if d.get("source_context") else None
    d["covers"] = json.loads(d["covers"]) if d.get("covers") else []
    d["cited_paths"] = json.loads(d["cited_paths"]) if d.get("cited_paths") else []
    d["ai_covers"] = json.loads(d["ai_covers"]) if d.get("ai_covers") else []
    return d


def replace_derived_requirements(
    repo_id: str, journey_id: str, run_id: str | None, requirements: list[dict], assessed_model: str | None = None,
) -> None:
    """Like replace_story_requirements, scoped to one journey's DERIVED rows
    only (`WHERE journey_id = ? AND source = 'derived'`) -- never touches a
    human-confirmed answer for this same journey (a prior derived row the
    human already resolved), another journey's rows, or any document-sourced
    requirement."""
    ts=now()
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=[dict(row) for row in conn.execute('SELECT * FROM story_requirements WHERE repo_id=? AND journey_id=?',(repo_id,journey_id))]
        key=lambda row:(' '.join(row['text'].split()),row['type'])
        used=set()
        for req in requirements:
            candidates=[row for row in old if row['id'] not in used and key(row)==key(req)]
            candidates.sort(key=lambda row:(row.get('retired',0),row['source']!='human_confirmed',row['created_at']))
            previous=candidates[0] if candidates else None
            req_id=previous['id'] if previous else str(uuid.uuid4())
            used.add(req_id)
            context=json.dumps(req['source_context']) if req.get('source_context') else None
            keep_human=bool(previous and previous['source']=='human_confirmed' and previous['run_id']==run_id and previous.get('source_context')==context)
            covers=json.dumps(req.get('covers') or [])
            values=(repo_id,journey_id,run_id,req['text'],req['type'],previous['status'] if keep_human else req['status'],
                previous['rationale'] if keep_human else req.get('rationale'),previous['covers'] if keep_human else covers,
                'human_confirmed' if keep_human else 'derived',req.get('question'),previous['created_at'] if previous else ts,
                ts,assessed_model,json.dumps(req['cited_paths']) if req.get('cited_paths') is not None else None,
                req['status'],covers,req.get('rationale'),context,req_id)
            if previous:
                conn.execute('INSERT INTO requirement_assessments VALUES (?,?,?,?,?)',(str(uuid.uuid4()),repo_id,req_id,ts,json.dumps(previous)))
                conn.execute('UPDATE story_requirements SET repo_id=?,journey_id=?,run_id=?,text=?,type=?,status=?,rationale=?,covers=?,source=?,question=?,created_at=?,assessed_at=?,assessed_model=?,cited_paths=?,ai_status=?,ai_covers=?,ai_rationale=?,source_context=?,retired=0 WHERE id=?',values)
            else:
                conn.execute('INSERT INTO story_requirements (repo_id,journey_id,run_id,text,type,status,rationale,covers,source,question,created_at,assessed_at,assessed_model,cited_paths,ai_status,ai_covers,ai_rationale,source_context,id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
        for row in old:
            if row['id'] not in used and (row['source']=='derived' or any(key(row)==key(req) for req in requirements)):
                conn.execute('UPDATE story_requirements SET retired=1 WHERE id=?',(row['id'],))


def confirm_story_requirement(requirement_id: str, status: str, rationale: str | None = None) -> dict | None:
    """A human answers a `needs_human_input` question: the row's status
    becomes whatever was confirmed and its source flips to 'human_confirmed'
    -- from this point it's indistinguishable from a document-sourced row
    everywhere else that reads story_requirements (rollup, citation, journey
    membership, test-case generation). This one field transition IS the
    "answers compound over time" mechanism -- nothing else needs to change.
    Deliberately never touches ai_status/ai_covers/ai_rationale -- those stay
    frozen at whatever the AI last computed, however many times this gets
    called on the same row (a second confirmation must not read the FIRST
    human's answer back out of `status` and mistake it for a fresh AI
    judgment -- see server/app.py's api_confirm_story_requirement)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE story_requirements SET status = ?, rationale = COALESCE(?, rationale), source = 'human_confirmed' WHERE id = ?",
            (status, rationale, requirement_id),
        )
    return get_story_requirement(requirement_id)


# --------------------------------------------------------------------------- human reviews

def record_human_review(
    repo_id: str, target_type: str, target_id: str, decision: str, note: str | None = None,
    ai_level_at_review: str | None = None,
    assessment_version: str | None = None, assessment_snapshot: dict | None = None,
) -> dict:
    """A durable, timestamped record of a human's own judgment on an AI
    assessment -- never overwrites the AI's own status/rationale/covers (see
    update_test_case_verification / replace_story_requirements), so a
    reviewer disagreeing with the AI is itself preserved, not lost. Read by
    server/evidence_rules.py (per-item evidence_level) and the review-accuracy
    endpoint (AI vs. human agreement rate). `ai_level_at_review` -- the
    caller's own evidence_rules.evidence_level(target), computed BEFORE any
    status-changing side effect of recording this review (e.g. confirming a
    story requirement) -- is stored as a permanent snapshot; see
    _HUMAN_REVIEWS_COLUMN_MIGRATIONS for why re-deriving it later is wrong."""
    review_id = str(uuid.uuid4())
    ts = now()
    with get_conn() as conn:
        if target_type == 'test_case' and assessment_version:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM test_cases WHERE id=? AND repo_id=?',(target_id,repo_id)).fetchone()
            if row is None:
                raise ValueError('Assessment changed; refresh before reviewing.')
            current=dict(row)
            for key in ('steps','covers','reassessment'):
                current[key]=json.loads(current[key]) if current.get(key) else None
            from .assessment_projection import assessment_version as version_for
            documents=[dict(row) for row in conn.execute('SELECT * FROM documents WHERE repo_id=?',(repo_id,))]
            requirements=[dict(row) for row in conn.execute('SELECT * FROM story_requirements WHERE repo_id=? AND COALESCE(retired,0)=0',(repo_id,))]
            for req in requirements:
                req['covers']=json.loads(req['covers']) if req.get('covers') else []
            if version_for(current,documents,requirements) != assessment_version:
                raise ValueError('Assessment changed; refresh before reviewing.')
        conn.execute(
            """INSERT INTO human_reviews (id, repo_id, target_type, target_id, decision, note, reviewed_at, ai_level_at_review, assessment_version, assessment_snapshot)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (review_id, repo_id, target_type, target_id, decision, note, ts, ai_level_at_review,
             assessment_version,json.dumps(assessment_snapshot) if assessment_snapshot else None),
        )
    return {
        "id": review_id, "repo_id": repo_id, "target_type": target_type, "target_id": target_id,
        "decision": decision, "note": note, "reviewed_at": ts, "ai_level_at_review": ai_level_at_review,
        "assessment_version":assessment_version, "assessment_snapshot":assessment_snapshot,
    }


def list_human_reviews(repo_id: str) -> list[dict]:
    """Every review for a repo, newest first -- a target reviewed more than
    once keeps every prior review too (a correction history), so callers that
    want "the current verdict" should take the first match per target_id."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM human_reviews WHERE repo_id = ? ORDER BY reviewed_at DESC, rowid DESC", (repo_id,),
        ).fetchall()
    return [{**dict(r), "assessment_snapshot":json.loads(r["assessment_snapshot"]) if r["assessment_snapshot"] else None} for r in rows]


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

class ActiveJobError(RuntimeError):
    pass


def create_bulk_job(repo_id: str, kind: str, total: int) -> dict:
    job_id = str(uuid.uuid4())
    ts = now()
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active=conn.execute("SELECT id FROM bulk_jobs WHERE repo_id=? AND status='running'",(repo_id,)).fetchone()
        if active:
            raise ActiveJobError('A bulk job is already running for this repository: '+active['id'])
        conn.execute("INSERT INTO bulk_jobs (id,repo_id,kind,status,total,processed,results_json,started_at,owner_pid) VALUES (?,?,?,?,?,?,?,?,?)",
                     (job_id,repo_id,kind,'running',total,0,json.dumps([]),ts,os.getpid()))
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
        row=conn.execute('SELECT * FROM bulk_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            return
        results=json.loads(row['results_json'] or '[]')
        if status=='success' and (row['processed']<row['total'] or any(not result.get('ok') for result in results)):
            status='failed'
            error=error or 'Work was incomplete or some entries failed; review the results before retrying.'
        conn.execute('UPDATE bulk_jobs SET status=?,error=?,finished_at=? WHERE id=?',(status,error,now(),job_id))


def recover_interrupted_jobs():
    """Recover dead-owner jobs without interrupting another live local worker."""
    with get_conn() as conn:
        for row in conn.execute("SELECT id,owner_pid FROM bulk_jobs WHERE status='running'").fetchall():
            alive=False
            if row['owner_pid']:
                try:
                    os.kill(row['owner_pid'],0)
                    alive=True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    alive=True  # Ownership cannot be established; do not kill live work.
            if not alive:
                conn.execute("UPDATE bulk_jobs SET status='failed',error=?,finished_at=? WHERE id=?",
                    ('Interrupted before completion. Review partial results and retry explicitly.',now(),row['id']))


def active_bulk_jobs(repo_id):
    with get_conn() as conn:
        ids=[row['id'] for row in conn.execute("SELECT id FROM bulk_jobs WHERE repo_id=? AND status='running'",(repo_id,))]
    return [get_bulk_job(job_id) for job_id in ids]


def get_bulk_job(job_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bulk_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["results"] = json.loads(d["results_json"]) if d.get("results_json") else []
    return d


# --------------------------------------------------------------------------- dashboard snapshots

def upsert_dashboard_snapshot(repo_id: str, date: str, summary: dict) -> None:
    """One row per (repo_id, date) -- INSERT OR REPLACE keyed on the unique
    index, so calling this more than once on the same day (every dashboard
    view does) updates that day's row in place rather than accumulating
    duplicates. A fresh id on every call is fine: the unique index is what
    enforces one-per-day, not the primary key."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO dashboard_snapshots (id, repo_id, date, summary_json, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo_id, date) DO UPDATE SET summary_json = excluded.summary_json""",
            (str(uuid.uuid4()), repo_id, date, json.dumps(summary), now()),
        )


def list_dashboard_snapshots(repo_id: str, limit: int = 90) -> list[dict]:
    """Oldest first (chart-ready order) -- the most recent `limit` days."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dashboard_snapshots WHERE repo_id = ? ORDER BY date DESC LIMIT ?",
            (repo_id, limit),
        ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["summary"] = json.loads(d.pop("summary_json"))
        out.append(d)
    return out


class GenerationInputChangedError(RuntimeError):
    """Generation input changed before its drafts could be saved."""


def replace_document_cases(repo_id, doc_id, run_id, cases, *, expected_document_fingerprint=None, replace=True, source_requirement_id=None):
    """A failed insert rolls back the deletion and every prior insert."""
    if not cases:
        raise RuntimeError('No valid replacement cases; existing suite preserved')
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        from .test_case_reassessment import documents_fingerprint
        doc=conn.execute('SELECT * FROM documents WHERE id=? AND repo_id=?',(doc_id,repo_id)).fetchone()
        if doc is None or (expected_document_fingerprint and documents_fingerprint([dict(doc)])!=expected_document_fingerprint):
            raise GenerationInputChangedError('Document changed or was deleted during generation; existing cases preserved. Refresh and retry.')
        generation_context={'scope':'document_generation','document_id':doc_id,'document_fingerprint':documents_fingerprint([dict(doc)])}
        if source_requirement_id:
            req=conn.execute('SELECT retired FROM story_requirements WHERE id=? AND repo_id=? AND doc_id=?',(source_requirement_id,repo_id,doc_id)).fetchone()
            if req is None or req['retired']:
                raise GenerationInputChangedError('Source criterion changed during generation; existing cases preserved. Refresh and retry.')
        if replace:
            conn.execute('DELETE FROM test_cases WHERE repo_id=? AND source_doc_id=?',(repo_id,doc_id))
        created=[]
        for case in cases:
            case_id=str(uuid.uuid4())
            conn.execute('''INSERT INTO test_cases
                (id,repo_id,module,run_id,title,category,steps,expected_result,preconditions,description,priority,covers,created_at,source_doc_id,created_via,target,status,rationale,generation_context,source_requirement_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (case_id,repo_id,None,run_id,case['title'],case['category'],json.dumps(case['steps']),case['expected_result'],
                 case.get('preconditions'),case.get('description'),case.get('priority'),json.dumps(case.get('covers') or []),now(),doc_id,'generated',case.get('target'),case.get('status'),case.get('rationale'),json.dumps(generation_context),source_requirement_id))
            created.append({**case,'id':case_id,'repo_id':repo_id,'module':None,'source_doc_id':doc_id,'run_id':run_id, 'created_via':'generated','generation_context':generation_context,'source_requirement_id':source_requirement_id})
        return created


def confirm_requirement_review(repo_id, requirement_id, status, rationale, expected_version):
    """Bind the human decision and its status update in the same transaction."""
    from .assessment_projection import requirement_version,requirement_is_stale
    from . import evidence_rules
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM story_requirements WHERE id=? AND repo_id=?',(requirement_id,repo_id)).fetchone()
        if row is None:
            raise ValueError('Requirement no longer exists')
        req=dict(row)
        for field in ('covers','ai_covers','cited_paths','source_context'):
            req[field]=json.loads(req[field]) if req.get(field) else (None if field=='source_context' else [])
        documents=[dict(doc) for doc in conn.execute('SELECT * FROM documents WHERE repo_id=?',(repo_id,))]
        latest=conn.execute("SELECT id FROM runs WHERE repo_id=? AND status='success' ORDER BY started_at DESC LIMIT 1",(repo_id,)).fetchone()
        if requirement_version(req,documents)!=expected_version or requirement_is_stale(req,documents) or evidence_rules.is_stale(req,latest['id'] if latest else None):
            raise ValueError('Requirement, document or analysis changed; refresh and reassess before confirming')
        decision='confirmed_supported' if status=='supported' else 'confirmed_contradicted'
        conn.execute('''INSERT INTO human_reviews
            (id,repo_id,target_type,target_id,decision,note,reviewed_at,ai_level_at_review,assessment_version,assessment_snapshot)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (str(uuid.uuid4()),repo_id,'story_requirement',requirement_id,decision,rationale,now(),
             evidence_rules.ai_evidence_level(req),expected_version,json.dumps(req)))
        conn.execute("UPDATE story_requirements SET status=?,rationale=COALESCE(?,rationale),source='human_confirmed' WHERE id=?",(status,rationale,requirement_id))
    return get_story_requirement(requirement_id)


def get_repo_gap_assessment(repo_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM repo_gap_assessments WHERE repo_id = ?", (repo_id,)).fetchone()
    return dict(row) if row else None
