# TestAtlas audit remediation

2 November 2026. Supersedes the unresolved-work status of the earlier readiness audit for the implementation items below; the original reproductions remain a historical record.

## Implemented

- Canonical assessment projection: one non-mutating interpretation supplies state, label, evidence level, agreement decision and version. Case display, review, filters, exports and dashboard evidence counts consume it. Original stored AI status and reports remain intact. Inference is still labeled separately and cannot be confirmed as an independent assessment.
- Version-bound reviews: the UI submits the version rendered with its buttons, not a newer cached object. Missing versions return 428; changed versions return 409. The database repeats the check under a write transaction before insertion. Case edits also invalidate versions. New reviews save the version and the displayed assessment snapshot. Historical reviews remain recorded and are identified after reassessment.
- Agreement must match the displayed finding. A correction through the API must explicitly identify correction intent and supply a comment; it cannot be confused with an ordinary Agree click. Stale assessments cannot be agreed with. Disagreement/information comments remain required, trimmed and bounded.
- Export parity: JSON now retains historical status, overall expected result, canonical assessment, dates/model/run, freshness and human review metadata. CSV/Markdown descriptions carry the assessment interpretation, provenance, stale/source warnings, review decision/comment and overall expectation while retaining ADO columns.
- Dashboard evidence parity: earlier unexplained conflict claims are inconclusive rather than AI conflicts. On the real 47-case verification-demo repository, all cases have a canonical version; none of its 13 earlier unvalidated claims count as an AI conflict through that projection.
- Metric definitions: reviewer agreement on definitive AI judgments uses only definitive reviewer decisions on definitive AI judgments. Agreement with uncertainty, information requests and unavailable/uncertain AI snapshots have separate counters/exclusions. The UI publishes the definitive denominator and explicitly distinguishes review feedback from independently measured accuracy.
- Source freshness: case reads, review validation, exports, story reads and dashboard reads compare recorded evidence-file hashes with the current local checkout. Changed/missing files mark results stale without another analysis run. Hashes use the same UTF-8/newline normalization as source retrieval, including Windows CRLF. Paths outside the source root are not read; oversized/unreadable evidence is treated as changed/unavailable. Read boundaries do not fetch or update remote repositories.
- Freshness scope is explicit: checks cover recorded evidence files, not unexamined code, new dependencies, remote branch changes or runtime configuration. Old assessments without hashes say current-file freshness is unknown. Dashboard and case views expose this limitation. Existing old cases need reassessment to acquire source manifests.

## Semantic contradiction boundary

The reproduced same-direction stock-release paraphrase is now rejected as opposition, for new validation and saved-result presentation. The guard is deliberately narrow: differing numerical quantities, negation, named subjects or restrictive conditions are not treated as equivalent merely because their verbs match.

All findings explicitly record that quotation checks establish provenance, not semantic correctness, and that human review is required. A quoted, well-formed AI explanation can still be semantically wrong outside the guarded case. The general model-accuracy concern is mitigated, not proven solved. No second prompt, classification table or synthetic unit test establishes general contradiction understanding. Independent developer/QA adjudication and representative real-model evaluation remain required before end-user accuracy claims.

## Validation

549 Python tests pass. The new cross-surface suite exercises all 27 combinations through case view, review, export filtering and aggregate dashboard counts; it also covers atomic review races, missing/stale tokens, case edits, prior reviews, inferred cases, export metadata, metric populations, physical source changes, missing files, path escapes, CRLF hashes, no network fetch on reads and same-direction contradiction claims.

JavaScript syntax checks pass, with presentation/combination assertions, seven real renderer/request assertions and three dashboard-trend scenarios. The request test verifies that the version from the rendered buttons is sent even when the cached case has already changed.

A controlled browser fixture confirmed that a historical unknown status with a projected conflict records Agree as confirmation of that displayed conflict. Screenshot: TestAtlas-audit-fixes-preview.png. Fixture judgments are synthetic; no AI provider calls were made for these checks. Existing real user cases, AI findings and human review rows were not rewritten. Startup adds review-version/snapshot columns without destructive migration.

## Running version

The fixed application runs at http://127.0.0.1:8136. The earlier application on 8134 and disposable UI fixture on 8135 could not be stopped through the existing process sessions: automatic approval review rejected the stop operation because sandbox approval is unavailable. Use 8136 for the fixes. The original older process on 8123 was already outside termination permissions.

Changes are local and have not been committed or pushed in this remediation turn. Existing-source reassessment and an independently reviewed semantic accuracy study have not been run automatically.


## Subsequent deep-sweep fixes

The additional 30 findings are addressed in [DEEP_SWEEP_REMEDIATION.md](DEEP_SWEEP_REMEDIATION.md), with 578 Python tests passing. Its validation and server-status notes supersede the earlier counts and running-version statement above.
