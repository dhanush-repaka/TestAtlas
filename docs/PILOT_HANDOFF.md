# TestAtlas internal advisory pilot handoff

Prepared 1 November 2026. This is a local advisory pilot, not a release approval or compliance certification.

## Completed implementation

- Preserved the existing uncommitted work on AI snapshots, human reviews, current/stale breakdowns, and conservative labels.
- Removed historical combined-result fallback from the AI-supported trend. Missing values are omitted, lines split at missing values, and unavailable latest values are identified.
- Added evidence schema version 2 to new dashboard snapshots. Historical AI-supported-rate points without that version are omitted because their calculation cannot be assumed equivalent.
- Inferred test-case judgments now carry assessment kind, underlying judgment source, requirement ID and run ID. UI badges identify inference; CSV/Markdown explain the source and that execution is not implied; JSON preserves provenance fields.
- Added a local lifecycle regression using actual parsing, storage and API routes with mocked AI responses.

## Validation performed

- Full Python suite: **464 tests passed** with isolated DATA_ROOT.
- JavaScript syntax: `node --check static/app.js` passed.
- Trend renderer: `node tests/test_dashboard_trend.js` passed three scenarios: mixed old/new snapshots, all legacy snapshots, missing values between current snapshots.
- Lifecycle: add repo, parse, add story, store a citation-validated manual assessment, import CSV test case, verify with mocked AI, record disagreement, edit source, reanalyze, observe stale results, reassess, export JSON/CSV/Markdown.
- The lifecycle test attaches an imported case to the document via a fixture database update to exercise document verification. It does not prove the UI offers that attachment; imported bulk verification is covered separately by existing tests.
- Tests use temporary databases and source fixtures; they do not assess or modify customer repositories.
- Live browser validation was **not completed**: the execution sandbox rejected binding a local listening socket. No paid AI evaluation was run.
- A pre-existing ResourceWarning occurs in test_screen_parts.py for an unclosed fixture file; it does not fail the suite.

## Run locally

See ../LOCAL_SETUP.md for prerequisites and platform-specific setup. From the repository:

```sh
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
DATA_ROOT=/absolute/path/to/pilot-data ./.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8123
```

Open http://127.0.0.1:8123. Choose an isolated pilot data root. The application's .env loader can enable AI providers; explicitly review configuration before live evaluation. Start on loopback. Do not assume the shared-password gate provides enterprise identity or repository authorization.

Run checks:

```sh
DATA_ROOT=/absolute/path/to/scratch-data PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover -s tests -t .
node --check static/app.js
node tests/test_dashboard_trend.js
```

## Backup and restore

Stop the application and any jobs before filesystem backup. Copy the entire configured DATA_ROOT (including data, run artifacts, workspace snapshots/clones and the saved-token encryption key). Preserve the corresponding application version and configuration separately; protect secrets. Local source repositories outside DATA_ROOT need their own backup. Do not copy a live SQLite file without SQLite's backup mechanism.

To restore, stop the application, restore the complete directory into a separate location, point DATA_ROOT at it, then start the same application version. Confirm repos, documents, test cases, run artifacts and token decryption before using it. Startup applies additive schema migrations; take a backup before upgrades and do not assume older binaries can read upgraded data. A clean-machine installation and real encrypted-token restore drill remain unverified.

## Pilot owner checklist: outstanding

1. Run the UI lifecycle on the target machine, including charts, evidence display, human review, exports and a hard page reload.
2. Check failure behavior through the UI: missing API credentials, provider failure, unsupported source files and interrupted/restarted background jobs. Existing automated tests do not establish that every user-facing recovery path is adequate.
3. Independently evaluate a representative real project. Use PILOT_EVALUATION.csv. Freeze commit, requirement version and tool/model configuration. Developers/QA record expected judgments before seeing TestAtlas output; resolve reviewer disagreement explicitly.
4. Include negative criteria, cross-file/service behavior, configuration dependencies, deliberately absent code and confusingly similar symbols. Measure false support, false conflicts, retrieval misses and incorrect test links. Report sample counts, exclusions and review effort; do not call unreviewed findings accurate.
5. Complete a clean installation and backup/restore drill on the intended host.
6. Assign support ownership and agree how pilot users report incorrect results.

## Scope and unverified controls

- Static and AI evidence is not executed line/branch coverage, proof of correctness, or a release guarantee.
- Citation overlap remains a suggested relationship. An inferred verdict is not independent test verification.
- Freshness compares analysis run IDs. It cannot detect all local edits or requirement changes between analysis runs, and reanalysis of unchanged code can conservatively mark results stale.
- Reviewer records do not establish authenticated reviewer identity; enterprise login, repository permissions and tamper-resistant audit controls require a separate assessment.
- Provider approval, exact outbound data, retention/deletion, concurrency, job restart recovery and organizational security requirements remain unverified for corporate deployment.
- No live AI accuracy study has been completed in this handoff. Unit test counts must not be used as model accuracy evidence.

No commit or deployment was created during this handoff. Review the combined diff, which includes prior work by Claude and the completion edits, before committing.
