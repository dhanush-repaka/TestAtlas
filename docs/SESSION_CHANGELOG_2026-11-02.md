# TestAtlas work summary and next-session handoff

Work spans 1–2 November 2026 UTC (the current work session crossed midnight). This summary starts with the false-confidence recommendations implemented through Claude, then includes the subsequent Codex work. Commit history verifies landed changes, not who authored every intermediate edit.

## 1. Initial recommendations implemented through Claude

- Reworded dashboard claims: AI-supported implementation, potential conflict and no implementation evidence found, rather than verified correctness or not started. PageRank-derived labels now say structurally central rather than business critical.
- Added assessment provenance: analysis/run identity, assessment time/model, source references and stale-result handling.
- Added separate, durable human reviews and review-accuracy infrastructure. Human actions do not overwrite test-case AI findings.
- Added test-case story/code linkage percentages, imported/generated dashboard breakdowns and an Imported filter. Imported cases remain imported after code linkage is resolved.
- Introduced source/evidence safeguards and supporting tests; these changes did not by themselves replace snippet retrieval or establish accuracy.

Verified commits: `51ad4ce` (origin/linkage dashboard), `ead75e9` (evidence-aware wording), `6ce9958` (provenance/reviews/freshness).

## 2. Coverage and evidence follow-up

- Preserved original AI story judgments separately from human-confirmed answers; excluded legacy rows without comparable AI snapshots from accuracy calculations.
- Separated AI-only story metrics from human-confirmed health, including the trend view.
- Unified fresh/stale linkage denominators, per-origin counts and unavailable states, avoiding misleading percentages when no fresh cases exist.
- Added evaluation checklists and handoff notes. These are evaluation infrastructure; no independent accuracy study has been completed.

## 3. Test Cases UI redesign

- Compact accordion rows; selecting another case closes the previous expanded case.
- Lazy detail rendering, clearer Imported/Generated origin and separate code/story/journey method labels.
- Composable search and filters; export selection matches the active scope.
- Grouped generation/import/assessment controls, persistent job progress and visible reassessment actions.
- Verified controlled fixtures in light/dark modes and at 390px width.
- Fixed module regeneration deleting imported cases after they had gained module links.
- Duplicate detection and the larger navigation/product rebuild remain backlog work.

## 4. All-origin assessment/reassessment

- Shared three-way assessment for initial assessment and reassessment: Test ↔ requirements, Test ↔ code, Code ↔ requirements.
- Per-case, matching-case and all-stale-case reassessment, plus existing imported/document routes.
- Assessment history preserves previous case state and new findings. Case content, origin, IDs and human reviews are retained.
- Optimistic content fingerprints reject concurrent test-content changes; batch omissions and invalid output are surfaced rather than silently counted as success.
- Document/case changes and newer recorded analysis runs invalidate results. Physical source changes are not continuously watched.

## 5. Expanded source context

- Replaced capped isolated-function retrieval with full relevant files, callers/callees, imports, lexical matches and referenced local configuration.
- Added Python and TypeScript/JavaScript symbol evidence.
- Bounds: 24 files, 160,000 source characters total, 40,000 per file, depth 3. Whole-file inclusion or explicit omission; this is not entire-repository ingestion.
- Recorded hashes, paths, ranges, inclusion reasons and limitations. Known retrieval gaps or model-declared missing context block definitive code judgments.
- Connected source context to Story Coverage, shared case assessment, code generation, journey flows, advisory Gap Analysis and manual context exports.
- Retained explicit notices for old results without recorded source scope; old judgments need reassessment.

Sections 2–5 are included in `3fb8a13`.

## 6. Reproducible cases and review visibility

- All generation paths now require an explicit starting point, initial state and setup steps before the scenario: evidenced home/login/navigation for UI cases, fixture/client/instance setup for API/library cases.
- Missing or ambiguous setup is rejected instead of inventing navigation. Setup does not inflate behavioural-depth checks or execute the behaviour under test.
- Existing cases are unchanged; regeneration is needed to add setup steps. Invalid module generation preserves the existing suite.
- Assessment prompts now include preconditions and description, numerical-state tracing, and separate specification alignment from code behaviour.
- Contradiction claims require exact opposing excerpts and a stated difference; unsupported/identical quotations are demoted. This validates structure/provenance, not semantic truth.
- Disagree is visibly recorded as Disputed by reviewer. Older reviews are identified as belonging to the prior assessment; AI findings stay separate.

Included in `4b7ebc5`.

## 7. Latest changes in this commit

- Renamed confirmation to Agree with assessment; added agreement with uncertainty as its own review decision.
- Added reviewer comments; Disagree and Request more information require comments in UI and API.
- Recorded uncertainty agreement separately in review statistics.
- Added explicit rules for all 27 supported/contradicted/unknown combinations, each with its own label and action.
- Known conflicts survive unknown comparisons. Dedicated diagnoses cover code/test requirement conflicts; inconsistent findings identify comparisons needing review.
- Combined diagnoses require overlapping acceptance-criterion quotations. Older unexplained claims display as Earlier conflict claims · reassess.
- Saved reports receive updated presentation without rewriting original findings or review history.
- Updated static asset cache version and restarted the local server at http://127.0.0.1:8134.

## Validation and remaining work

Latest verification: 533 Python tests passed; JavaScript syntax, presentation/combination tests, three dashboard-trend scenarios and whitespace checks passed. These are mechanics/integration checks with controlled model outputs, not a live-model semantic accuracy study.

The subsequent audit reproduced six unresolved correctness gaps: same-direction paraphrases accepted as conflicts; displayed outcome versus review decision mismatch; no review-to-assessment version binding; export metadata loss; dashboard versus projected-case mismatch; mixed agreement denominators. It also confirmed the known source-freshness boundary.

See `READINESS_GAP_AUDIT.md` for reproductions, severity and completion gates. No fixes to these six findings were applied during the audit. Some gaps were introduced or exposed by the recent display/review changes; they must be addressed across consumers, not through more labels alone.

Current recommendation: supervised developer/QA evaluation only. A corporate end-user pilot-readiness recommendation is withheld until the consistency gaps and independent semantic evaluation are addressed. Identity, permissions, source handling and reviewer attribution require a separate deployment review.

Next session: prioritize canonical assessment state shared across all consumers; assessment-version-bound reviews; faithful exports/dashboard counters; well-defined review metrics; then semantic false-positive evaluation and source snapshot visibility. Preserve existing findings/reviews and verify initial assessment → review → change → reassessment → export as one workflow.

Separate pre-merge audit and IDE-extension tools remain separate product ideas; neither was built in this session. No external deployment is part of this local commit request.
