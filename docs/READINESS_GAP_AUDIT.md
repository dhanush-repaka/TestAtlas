# TestAtlas readiness gap audit

Audit date: 2 November 2026. Scope: current local checkout, including the recent review and 27-combination classification changes. This is a targeted correctness audit, not a complete product or security certification.

## Readiness decision

Not ready for a corporate end-user pilot that relies on dashboard assessments. Suitable only for supervised developer/QA evaluation with independent expected answers and no release decisions based on TestAtlas findings. Earlier pilot advice needed this explicit qualification and acceptance evidence. Passing 533 tests establishes tested code behaviour, not model accuracy or consistency across every product surface.

Six correctness gaps were reproduced using synthetic data and temporary databases. A seventh item is a confirmed, previously documented freshness boundary. No production cases, assessments, or human reviews were changed. No live AI calls were made. No fixes were applied during this audit.

## Findings

### 1. High: structural conflict validation still accepts same-direction statements

Reproduction: test expectation `Stock increases after release.`; requirement `Available stock increases when reserved stock is released.` Supply both exact excerpts and an explanation alleging opposition. The validator accepts `contradicted`; the overall classifier returns `conflicts_found`.

Cause: `server/test_case_reassessment.py:47` verifies quotation provenance, a nonempty explanation, and that the two strings are not identical after normalization. It does not establish semantic opposition. The new combination table faithfully propagates a potentially wrong input judgment.

Required response: distinguish a structurally validated AI claim from a human-adjudicated conflict. Build independent, labeled positive/negative examples including paraphrases, quantities, preconditions, negations and distinct acceptance criteria. Any further model check must itself be evaluated; another prompt is not proof. Require review for consequential use. Do not claim this gap is solved by exact quotations.

### 2. High: displayed diagnosis and Agree action can disagree

Reproduction: a saved report has old overall `unknown` but validated comparisons C/S/C. The UI now correctly displays `implementation_and_test_conflict`. The review endpoint still accepts `agreed_uncertain` and reports `Reviewer agrees evidence is insufficient`.

Cause: display derives a new outcome; UI review decisions and API checks still use stored `status`. See `server/test_case_view.py:16`, `static/app.js` review actions, and `server/app.py:1514`.

Required response: one canonical assessment projection for display, review, exports and dashboard. Preserve the original stored judgment separately. Save the exact displayed outcome and assessment version with each review.

### 3. High: review submission is not bound to the assessment the reviewer saw

Reproduction: a reviewer opens a supported assessment; it changes to contradicted before submission. Submitting `confirmed_supported` succeeds with HTTP 200 and is recorded against `ai_conflict`.

Cause: submission contains no expected assessment ID/version. Existing API semantics also permit explicit opposite judgments, so a stale Agree click can be misinterpreted as an intentional correction. See `server/app.py:1508` and `server/db.py:1063`.

Required response: send an immutable assessment ID or version/fingerprint and verify it atomically when saving the review. Reject a changed assessment with 409 and request a fresh review. Keep explicit correction actions distinct from agreement.

### 4. High: exports lose information needed to interpret findings

Reproduction: export a disputed, stale case with a reviewer comment. CSV and Markdown omit dispute/comment and freshness warning. JSON omits `status`, `expected_result`, `human_review`, `review_state`, `assessed_at`, and `run_id` (although it includes the three-way report and `is_stale`). An overall expected result is not necessarily recoverable from step expectations.

Cause: export serializers select only part of the case metadata. See `server/test_export.py:140` and `server/test_export.py:213`.

Required response: make JSON a faithful case-and-assessment export; add assessment status, provenance, freshness, review decision and comment to human-readable exports without breaking ADO columns. Verify export/view parity for disputed, uncertain, stale, inferred and legacy cases.

### 5. High: dashboard evidence counts can disagree with the case view

Reproduction: the case view labels older unexplained contradictions `unvalidated_conflict_claims`; `evidence_rules.evidence_level` still counts a stored contradicted status with covers as `ai_conflict`.

Current verification-demo data: 47 cases; 13 display earlier unvalidated conflict claims, and 4 of those still have raw `contradicted` status. This confirms the mismatch has applicable live records; it is not only a synthetic edge case.

Cause: dashboard evidence summary reads raw status/covers instead of the projected assessment. See `server/evidence_rules.py:57` and `server/traceability_dashboard.py:162`.

Required response: use the same canonical projection for all product counts, with a separate count for legacy unvalidated claims. Code citations alone must not upgrade a legacy claim into a validated conflict.

### 6. Medium: agreement percentage mixes different review populations

Reproduction: one agreement with supported evidence plus one agreement with insufficient evidence produces `total_reviews=2`, `agreed_with_ai=1`, `agreed_uncertain_count=1`, and `agreement_rate_pct=50.0`. Both reviewers selected agreement, but the metric is not an all-agreement rate. Nor is it a clean accuracy estimate for definitive judgments.

Cause: numerator excludes uncertainty agreement, denominator includes it. See `server/app.py:1579`.

Required response: define separate metrics for agreement with definitive judgments, agreement with uncertainty, disagreements and requests for information. Explicitly publish denominators and exclusions; none should be labeled measured model accuracy without an independently adjudicated sample.

### 7. Known limitation: filesystem changes do not automatically invalidate an assessment

Reproduction: change a local source file after assessment without running analysis. `is_stale` remains false.

Cause: freshness follows recorded analysis runs and document/case fingerprints, not continuous filesystem changes. This boundary was previously documented.

Required response before live project-status claims: show which recorded source snapshot freshness refers to. Add source-version comparison at controlled analysis/review boundaries or require a visible analysis-before-assessment workflow. A watcher is optional; honest snapshot state is essential.

## Why the prior checks missed these

- Tests covered individual functions and the intended label changes, but did not enforce agreement between UI, review, export and dashboard representations of the same record.
- The 27-combination tests cover the classification table once statuses are supplied; they do not establish that AI assigned those statuses correctly.
- Synthetic model responses verify prompt plumbing, source quotations and preservation. They do not measure false contradiction or false support rates.
- Reclassification changed presentation without updating all consumers of the old overall status. That is an integration gap in the recent work.

## Required completion gates

1. Define one canonical assessment representation, preserving original findings and distinguishing unsupported claims, source evidence, human decisions and runtime results.
2. Fix review/version binding, export parity, dashboard parity and metric definitions. Add cross-surface lifecycle tests rather than only tests mirroring each implementation.
3. Exercise initial assessment → review → code/story change → reassessment → old-review display → export. Include concurrent assessment and review submission, all 27 combinations, no citations, inferred cases and old records.
4. Independently label a representative real-project set with developer and QA review, including deliberately wrong AI findings. Measure false support, false conflict, missing evidence and review effort by category. Agree acceptable limits before evaluating.
5. Reassess old results explicitly; never silently rewrite their historical AI findings or reviewer decisions.
6. Revisit corporate deployment requirements separately (identity, access, source handling and audit attribution). The current shared-password mechanism does not establish reviewer identity.

The audit does not establish absence of other defects. These reproduced findings are enough to withhold an end-user pilot-readiness recommendation now.
