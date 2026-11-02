# TestAtlas dashboard rebuild

2 November 2026. This supersedes the dashboard portion of the earlier UI refresh. The dashboard has a new composition and native chart system; other tabs retain their workflows.

## Why the acceptance-criteria canvas was blank

The inspected API response reported 9 acceptance criteria, `by_status` all zero, `ai_unavailable=0` and `evidence.stale=9`. The total includes stale criteria, while the by-status counts include only fresh automated judgments. The old renderer put 9 in the heading but selected zero positive bars, leaving an empty canvas.

The new chart partitions the total into fresh supported, conflict, human-input, inconclusive, unavailable and stale categories. Those nine criteria now visibly fill the Needs reassessment segment. Stale results are not counted as current support, and overlapping historical unavailable/stale flags are not double-counted. A legacy payload with missing classifications is shown as unavailable; inconsistent totals produce an explicit reconciliation warning.

The old empty-state code also replaced the canvas parent contents. The new SVG renderers retain their host elements, so switching from an empty project to a populated one, reloading, and changing themes can render again safely. Dashboard charts no longer require Chart.js or its CDN script.

## Visual direction

- Product: project evidence for developers, QA and leads.
- Character: precise, confident, editorial.
- World: an engineering atlas, with clear records and traceable links.
- Moments: a large criteria-status graphic and two case-linkage lanes.
- Signature: consistent visual treatment for requirements, code and test cases.

The user's request for a complete new dashboard authorized recomposing sections. The approved neutral/teal brand remains, while the chart hierarchy and layout change substantially.

## Nine passes

1. **Spacing:** a 24px dashboard rhythm, aligned headings, figures and chart legends. A compact status disclosure replaces the large prose banner; charts precede the scope inventory.
2. **Color:** neutral chrome; meaning-bearing color in the criteria chart; restrained teal for linked cases. Stale evidence has a diagonal hatch so it differs by texture as well as color.
3. **Icons:** removed decorative dashboard icon clutter and replaced chart-library shapes with a consistent native SVG treatment.
4. **Copy:** project-specific labels such as Project evidence, Case linkage and Scenario balance. Repeated AI branding is removed from prominent cards; automated/AI assessment provenance remains clear in captions and methodology.
5. **Type:** large criterion total, aligned monospace counts, section indexes, and a restrained title hierarchy. Bar-chart labels remain HTML text rather than shrinking inside an SVG viewBox.
6. **Layout:** dominant criteria panel beside linkage lanes; a separate scope strip; story status; scenario/priority comparisons; provenance; conflicts and story records; then history and methodology. Details and existing drill-down routes remain accessible.
7. **Details:** native SVG titles and accessible chart descriptions; explicit fresh denominators and excluded stale counts; missing priorities/categories are included as unspecified/unclassified. Missing current judgments show a dash in assessment signals rather than a misleading 0/0 rate.
8. **Visuals:** a segmented evidence ring, endpoint linkage graphics, lollipop comparisons, stacked provenance/status bars and the existing historically honest trend. All graphics use actual dashboard data. No illustrations, fabricated history or paid generation.
9. **Motion:** static, readable charts with theme tokens; no looping chart animation, growing-layout animation or CDN-dependent canvas redraw.

## Preserved boundaries

Source-only metrics continue to read the frozen automated snapshot. Human confirmations cannot increase source-supported counts. Story current-state views explicitly include human decisions. Links remain traceability, not proof of sufficient assertions or execution coverage. The dashboard says no test run or passing result is recorded. Empty risk lists no longer claim every criterion matches the code when assessments are stale/unavailable.

## Validation

- 595 Python tests passed using isolated temporary data.
- 36 native-chart assertions passed: the exact all-stale reproduction; empty to populated; mixed categories; stable repaint; legacy unavailable values; inconsistent totals; human-decision separation; fresh linkage denominators; missing case priorities/categories; stable chart hosts; removal of the chart CDN.
- 11 additional source-signal/PDF-state assertions passed, including expansion and restoration of every export detail section.
- Existing dashboard trend, case/review/conflict presentation, review-version race, Story Coverage snapshot/provenance, polling recovery and tool-discoverability checks passed. JavaScript syntax and diff whitespace checks passed.
- Browser inspection at 1280px confirmed the real 47-case preview, a chart labeled Needs reassessment 9, no dashboard canvases, no Chart.js CDN script, stable criterion data after theme switching, and no page-level horizontal overflow.
- Both dark and light themes were visually checked. Responsive rules are implemented, but a new exhaustive mobile/device matrix and an actual PDF file render were not performed in this pass.

## Preview

Refresh http://127.0.0.1:8144/repo/e58e74f9-54ff-4414-a45e-53807dcea948/traceability . It is the existing temporary-data preview; the original database and cases were not rewritten or reassessed. No live model calls were made. Changes remain local and uncommitted; no push/deployment occurred.

Previews: TestAtlas-dashboard-v2-dark.png, TestAtlas-dashboard-v2-light.png, TestAtlas-dashboard-v2-detail.png.

Method: unslop by Damir Sokolovsky (@heydamir), https://github.com/heydamir/unslop, CC BY 4.0. No image prompt or story video was needed.
