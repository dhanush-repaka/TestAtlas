# TestAtlas source-evidence audit and implementation

2 November 2026

The original source-context gap was real. Earlier changes improved labels, staleness, review history and three-way comparison; they did not resolve capped isolated-function retrieval. This work expands evidence gathering and makes its remaining limits inspectable. It does not establish corporate release readiness or model accuracy.

## Assessment-path audit

| Path | Earlier input | Current behaviour |
|---|---|---|
| Automatic Story Coverage | Retrieved modules, up to 40 Python function snippets | Relevant full files, caller/callee and import expansion, lexical recovery, exact source quotes, stored manifest and conservative context gate |
| Imported, document, individual and group case assessment | Same capped snippets | Shared expanded bundle; code findings become unknown when a known retrieval gap or model-declared gap remains; independent requirement findings remain separate |
| Code-generated case drafts | Module snippets plus graph/documents | Expanded full-file prompt; module draft generation records its evidence manifest; generation is not an assessment or executed test |
| Journey-derived requirements | Reachable function snippets | Expanded context, source-quote validation and context gate; the result still describes code-derived candidate requirements, not independently established business intent |
| Journey case generation | Requirement statuses plus snippets | Expanded context in prompt; statuses inherited from requirements remain inherited evidence and should be independently reassessed |
| Story/requirement case generation | Raw story or previously assessed requirement | Deliberately unchanged: generation itself is not implementation verification |
| Automatic Gap Analysis | Names/structure only | Expanded context and manifest; UI and prompt explicitly identify advisory candidate gaps, not proof that implementation is absent |
| Manual context export/paste | Mostly names/structure | Story and gap context exports now include expanded evidence. Pasted judgments remain unrecorded-source/manual input; the tool cannot prove the external reviewer followed the retrieval process |
| Dashboard | Stored judgments and structural links | No new correctness proof inferred. Existing AI/human separation and freshness rules remain; source-scope notices identify older assessments |

## Retrieval and evidence policy

`kg/source_evidence.py` supplies the shared bundle. It starts from selected graph files, adds deterministic query matches, and follows caller/callee and import edges in both directions. It also recovers explicit local Python imports and literal configuration references from current source. Complete selected files expose globals, class setup, decorators, routes and neighbouring functions that isolated snippets omitted. Python and TypeScript/JavaScript symbol bodies support exact-quote validation.

Default limits: 24 files, 160,000 source characters total, 40,000 characters per file and three dependency levels. A file is included whole or omitted with an explicit reason. This is not full-repository ingestion. Retrieval remains heuristic and can miss relevant code.

Manifests record paths, line ranges, content hashes, inclusion reasons, bounds and known limitations. Missing/unreadable files, parser omissions, unresolved imports/calls, dynamic dispatch, runtime settings, ambiguity and budget/depth boundaries prevent definitive automatic source findings. The model must also explicitly report context sufficiency and no missing context. A model assertion cannot override a known bundle gap. This is conservative at bundle scope and may increase unknown results, including when a limitation later proves irrelevant to a particular criterion.

The manifest's internal `context_complete` field means only that the bounded retrieval found no known gap. The UI says “no detected retrieval gaps,” never “complete system verified.” Runtime and deployment behaviour are explicitly unverified.

Secret-like configuration files, `.env` files and paths escaping the source root are excluded. Exclusions remain evidence limitations; deployment environment values are never assumed. Credential detection is heuristic, not a security audit or a comprehensive secret scanner.

## Persistence and presentation

Story requirement source manifests survive database reads and human confirmations. Case reassessment reports/history contain their bundle manifest and source citation locations. Module-generated drafts store generation context separately. Gap findings store their advisory context. JSON case exports include reassessment and generation evidence.

The UI shows examined files, hashes, source ranges, reasons and retrieval limits in an expandable Source context section. Earlier judgments are not silently upgraded; they show an unrecorded-source notice. Rerun Story Coverage and reassess cases to obtain expanded evidence. Run analysis first after code changes.

## Verification

507 Python tests pass, with JavaScript syntax, presentation and trend checks and whitespace validation. Seventeen new retrieval/evidence regressions cover helpers and callers, a bypass caller, globals, more than 40 functions, literal configuration, secrets exclusion, environment settings, injected dependencies, missing imports, depth/size limits, lexical recovery, TypeScript bodies, symlink escapes, model-declared gaps, three-way gating, and persisted story evidence.

Browser fixture: actual retrieval of service.py, entry.py and policy.py; an unavailable ORDER_LIMIT setting appears as a limitation and the case stays Needs clarification. Judgments in browser/test fixtures are controlled; no paid model calls were made against real user cases. Automated tests verify mechanics and evidence boundaries, not model semantic accuracy.

## Still required before confidence claims

Independent developer/QA evaluation on representative real criteria, live-model false-support/missed-evidence measurements, and executed tests for runtime/integration claims. See the evaluation checklist. Do not describe the original false-confidence problem as fully solved.
