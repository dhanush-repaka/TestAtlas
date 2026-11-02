# TestAtlas deep-sweep remediation

2 November 2026. Supersedes the unresolved status of the 30 findings in TestAtlas-deep-sweep-report.md. Changes are local and uncommitted.

## Implemented and checked

| Findings | Correction |
|---|---|
| DS01–DS04 | Document revisions participate in requirement freshness and review versions. Story confirmation requires the displayed version. Case review repeats document/requirement checks inside the insertion transaction. Inferred findings change version when their inputs change. |
| DS05–DS07 | Failed or omitted assessments prevent a job reporting success. Unexpected worker exceptions finish the job as failed. Valid partial results remain saved. |
| DS08 | Document-suite replacement deletes and inserts inside one transaction. Insert failure rolls back to the original suite. Empty/incomplete replacement preserves existing cases. |
| DS09, DS25 | Matching requirement text/type retains its ID; superseded requirements are retired and previous judgments archived. Matching human-confirmed criteria are reconciled rather than double-counted. |
| DS10 | Disagree requires an existing finding. Information requests remain available for unassessed cases. |
| DS11, DS23 | Startup marks jobs owned by terminated processes failed. A running job prevents overlapping metered bulk jobs in the same repository; the UI can resume polling it. |
| DS12–DS13 | Unresolved JavaScript receivers and computed Python file reads add explicit source-context limitations, preventing definitive code findings from known incomplete context. |
| DS14 | The narrow release-direction guard distinguishes available, reserved and total stock subjects. |
| DS15–DS16 | CSV/XLSX imports retain structured cells, including pipes/newlines, detect each worksheet header and skip repeated headers. |
| DS17 | JSON exports preserve raw setup-step kinds and exact source IDs. |
| DS18 | Malformed model covers fields become dropped results with an explanation rather than uncaught TypeError. |
| DS19 | Configured passwords protect project reads by default. Anonymous browsing requires explicit TESTATLAS_PUBLIC_READ_ONLY opt-in. Password-free local usage remains available. |
| DS20 | Invalid office files return validation errors. Upload/expanded ZIP limits apply; valid large CSV cells parse without the old default field-size failure. |
| DS21 | Story filters include the displayed direct and suggested links consistently. |
| DS22 | Repository updates validate source types and required source settings. |
| DS24 | Repository deletion also removes dependent dashboard snapshots and requirement archives. |
| DS26 | Generation returns accepted/rejected/truncated diagnostics without changing list-shaped API responses. Warnings persist in the test-case panel. Incomplete replacements are refused. |
| DS27 | Polling retries transient transport failures and displays connection loss; a later response restores progress. |
| DS28 | A delayed review response cannot mark a newer cached finding confirmed. |
| DS29–DS30 | Structured imports bypass document-text truncation. Truncation markers, blank scenarios and incomplete action/expectation steps are rejected; metadata-only rows followed by valid steps remain supported. |

## Compatibility and data handling

Existing case content and human-review records are not mass-rewritten. Schema migrations are additive. Historical AI judgments stay separate from human feedback; agreeing never converts source assessment into recorded test execution. Existing list-shaped generation/import responses remain lists. JSON exports add provenance fields. Selected source-file bodies can be sent to the configured AI provider; README, LOCAL_SETUP and .env.example now state this explicitly.

Intentional stricter behavior: stale/missing-version review submissions fail with actionable responses; incomplete suite replacement is refused; incomplete jobs finish failed rather than success; malformed imports fail validation; configured passwords protect reads unless public browsing is explicitly enabled.

## Validation

- Full Python suite: **578 tests passed** (549 previous tests plus 29 new deep-sweep regressions).
- JavaScript: syntax check passed; review renderer/request race (8 assertions), polling recovery (5), test-case presentation (9), human-review presentation (4), conflict presentation (6), combination label, and dashboard trend (3 scenarios) passed.
- git diff --check passed.
- Tests used isolated temporary data and controlled model responses. No paid/live AI calls or automatic reassessment of user cases were performed. UI races were checked against real functions in Node VM fixtures; a fresh interactive browser pass of this final batch has not been performed.

## Remaining boundaries

The 30 reproduced implementation findings have fixes and regression coverage. This is not proof of general model accuracy or production readiness. Source retrieval remains bounded and cannot establish runtime behavior, unseen integrations or full-repository completeness. Exact quotations prove provenance, not semantic correctness. Real-project developer/QA adjudication and deployment-specific identity/security evaluation remain necessary. Shared-password access is not per-user corporate identity.

This batch has not restarted any existing server. Previously running processes may still serve earlier code; restart the application before manual validation. Changes have not been committed or pushed.


## Follow-up pass

Eight additional reproduced paths were fixed in [SECOND_SWEEP_REMEDIATION.md](SECOND_SWEEP_REMEDIATION.md). The current full suite has 595 passing Python tests; see that report for scope and validation limits.
