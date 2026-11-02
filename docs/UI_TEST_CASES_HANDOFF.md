# Test Cases UI update

Implemented 1 November 2026 on top of the existing uncommitted TestAtlas changes.

## Behavior

- The default Test Cases view is a compact suite list. Native details/summary controls expand one case inline at a time, with a JavaScript fallback enforcing one open case.
- Steps and evidence are rendered on demand rather than creating every expanded case body at startup.
- Imported/Generated origin is always visible and remains independent of code/story/journey generation method.
- Search, origin, module, assessment, category, generation method and source-story filtering compose. CSV, Markdown and JSON exports use the same scope rules; JSON now includes case ID and origin.
- Import and generation/assessment controls are grouped behind Add cases & assess. Job progress remains outside that section so it can stay visible when management controls are closed.
- Source assessment, inference, freshness and recorded human decisions remain distinct. Expanded cases show rationale, citations, available cited paths, assessment metadata and the latest recorded review.
- AI actions are disabled when no provider is configured. Importing remains available. No AI call is made by opening cases or filtering them.
- Fixed an existing regeneration bug: code-generated module refresh now excludes imported cases from deletion, even if those imported cases have acquired module/code links.
- Duplicate detection and the wider five-destination navigation redesign remain separate work; no illustrative duplicate candidates were added to the application.

## Verification

- Full Python suite: 470 tests passed, using isolated DATA_ROOT.
- `node tests/test_test_case_ui.js`: 9 presentation-rule assertions passed.
- `node tests/test_dashboard_trend.js`: 3 scenarios passed.
- `node --check static/app.js` and `git diff --check`: passed.
- Real browser checks against a loopback test instance with disposable fixture records: expand/switch/collapse cases, origin and assessment filtering, composed filters, search empty state, grouped management controls, missing-provider gates, light/dark appearance and 390px layout. The 390px page had no document-level horizontal overflow.
- The isolated server stalled once on API reads; restarting that test instance restored normal loading. The cause was not established. It was not reproduced during the subsequent final checks.
- Screenshots use disposable fixture data. No production repository records were modified and no live AI calls were made for this UI verification.
- Existing Python ResourceWarning in test_screen_parts.py remains unrelated to this change.

## Loading the change

The application reads its HTML shell at process startup. Restart the existing TestAtlas server using its usual command/environment, then refresh the browser. An old process may serve the old HTML alongside updated static assets until restarted. Updated static asset query versions refresh browser caches.

No commit or deployment was created. The working tree still includes Claude's earlier work and the reporting fixes completed before this UI task. The pre-UI diff was saved in this chat's work/testatlas-before-ui.patch.
