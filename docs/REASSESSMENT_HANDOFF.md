# TestAtlas reassessment workflow

Implemented 1 November 2026. Existing cases are preserved; no generation or test execution is triggered by reassessment.

## User workflow

After code changes, run analysis. In Test Cases use Reassess all stale cases (entire repository), Reassess matching cases (current filters), or expand a case and use Reassess this case. Assessment history shows prior case state and each new assessment. Existing import/document assessment actions share the enhanced verifier.

Each origin receives three independent AI findings: test versus current requirements, test versus available source bodies, and implementation versus the same requirement. Exact story/source quotations are validated against supplied evidence. Missing, ambiguous, or unsupported evidence becomes unknown. Ambiguous code names are excluded from source evidence.

The server derives conservative outcomes: potential implementation regression; test case needs updating; outdated test case (a previously aligned, cited requirement changed); alignment found; or needs clarification. Overall supported requires all three supported, using overlapping criterion quotations. No automatic rewriting or fix acceptance is implemented; updating content remains a human decision.

## Storage and APIs

Additive SQLite migration: test_cases.reassessment JSON and test_case_assessments history table. Every verifier patch archives previous case state and new findings in the same transaction; optimistic content fingerprints reject edits made during an assessment. IDs, expectations, steps, origin, and human reviews remain intact.

POST /api/repos/{repo_id}/test-cases/reassess accepts case_ids or stale_only. Background jobs batch 15 cases and use the existing verification-job polling endpoint. Invalid/foreign IDs are rejected. Omitted cases retain previous state and cause a partial job failure instead of silent success. GET /api/repos/{repo_id}/test-cases/{case_id}/assessments returns history.

Run changes and document/content fingerprint changes mark cases stale. Dashboard freshness uses the same metadata. JSON exports include the reassessment report and stale flag.

## Validation

490 Python tests pass, plus JavaScript presentation/trend tests, node syntax validation, and git diff whitespace checks. Coverage includes triage decisions, fabricated/missing evidence, different criteria, ambiguous code names, mixed origins, preservation of content/reviews, concurrent content edits, omitted cases, repository isolation, and a real verifier route with only the OpenAI SDK response mocked.

Browser checks: stale group job through real worker/storage/polling with controlled AI responses in disposable data; individual controls; three findings and history; desktop light/dark; mobile at 390px with document width also 390px. Actual repository at port 8134 loads new controls and existing cases. No paid AI calls were made on real user cases.

## Limits

AI semantic accuracy still requires evaluation with real stories and code; exact quotations alone do not prove the model interpreted them correctly. Source retrieval is bounded. Existing source_lookup only extracts Python function bodies, so unavailable source bodies (including unsupported languages) cannot receive a definitive code finding. Run analysis after code changes; this is not an IDE watcher. Document freshness conservatively invalidates assessments when any repository document changes. Existing legacy assessments are not silently upgraded; users must reassess them. Cases are not automatically edited and executable tests are not run.

The actual server is running at http://127.0.0.1:8134. The old 8123 process remains outside the sandbox's termination permissions. Temporary UI fixture server 8135 was stopped.
