# TestAtlas second-sweep fixes

2 November 2026. Addresses the eight reproduced findings in TestAtlas-second-gap-check.md. Changes remain local and uncommitted; existing servers were not restarted.

## Fixes

1. **Story Coverage AI counts:** the API exposes the shared AI evidence classification. The summary uses frozen AI snapshots, excludes stale support, reports unavailable/uncertain results and stale criteria separately, and states its denominator. Human confirmation no longer moves the AI-supported number.
2. **Work-item cell integrity:** CSV/XLSX imports read structured cells and per-sheet schemas, preserving pipes and embedded newlines. Repeated headers are skipped; malformed rows are rejected before any documents are created. Large structured imports avoid document-text truncation. Non-tabular document fallback remains supported; explicitly truncated fallback text is rejected.
3. **Generation input race:** document-generated drafts are saved in a single transaction that validates the document revision before inserting or replacing cases. Changed/deleted input returns a conflict and preserves the suite. This covers synchronous additive/replacement generation, criterion generation and bulk document generation. Drafts record document provenance and display it separately from code assessment.
4. **Stale story confirmation:** the database now checks the latest successful analysis run inside the same write transaction as the review and human decision. An old-run assessment cannot be confirmed even with its current display token, including a run completing between route validation and insertion.
5. **Inferred freshness:** inferred cases inherit supporting requirements' stale state and recorded source context. Document edits and recorded source-file changes propagate through case views and export filters. Dashboard inferred linkage is enriched after inference so its freshness follows the same policy; inference remains distinct from independent assessment evidence.
6. **Journey requirement identity:** matching derived criteria retain their IDs. Previous judgments are archived; superseded derived criteria are retired rather than deleted. Existing case links and human reviews still resolve. Matching confirmed criteria are reconciled without duplicate active rows; existing intentional preservation of unmatched human answers remains.
7. **JavaScript dependency limits:** source collection resolves literal object members using the existing tree-sitter parser. An object declaration alone no longer makes arbitrary methods resolved. Reassignment and runtime-global dependencies remain unavailable; normal locally defined methods and arrow members remain usable. These are bounded static checks, not a complete JavaScript runtime model.
8. **Journey model validation:** root, collection and covers types are checked. Malformed entries return controlled validation errors rather than TypeError. A partially invalid derived-requirement response is refused before replacing existing results, with reasons to retry.

## Verification

- **595 Python tests passed**, including 17 new second-sweep regressions. The previous 578 tests also remain passing.
- Real JavaScript functions: Story Coverage summary and document-draft provenance (12 assertions), review race (8), polling recovery (5), case presentation (9), review presentation (4), conflict presentation (6), combination label, and dashboard trends (3 scenarios) passed.
- JavaScript syntax and git diff whitespace checks passed.
- The original controlled reproductions were rerun against the fixes: old-run confirmation returns 409 with no saved review; an inferred case is stale when its requirement is stale; regenerated journey IDs remain stable with one active matching criterion; document-change replacement preserves the existing case; the real acceptance criterion survives pipe-containing work-item import; unresolved runtime member context is incomplete; human confirmation leaves AI support at 0% rather than changing it to 100%.
- Isolated temporary data and mocked model responses were used. No live AI calls, mass reassessment, or rewriting of user case/review records occurred. This batch was not checked in a new interactive browser session.

These fixes close the eight reproduced scenarios. They do not establish universal model accuracy or eliminate the need for representative developer/QA evaluation and deployment-specific checks before release. Restart the app to load the backend changes and reload the browser; the frontend asset version has been bumped.
