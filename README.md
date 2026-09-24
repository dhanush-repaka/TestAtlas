# TestAtlas

Turns a codebase into a **knowledge graph** — File/Class/Function nodes,
IMPORTS/DEFINES/CALLS edges — using real syntax-tree parsing (zero cost,
deterministic): **Python** via the stdlib `ast` module and **TypeScript /
JavaScript** (`.ts .tsx .js .jsx .mts .cts .mjs .cjs`) via tree-sitter, all
into the same graph schema, so a repo can mix languages. Then layers two
things on top:

- **Business-module criticality scores** (PageRank + betweenness centrality via
  networkx, rolled up from files to the business capability they implement --
  Accounts, Payments, Statements -- not one score per file). The free default
  groups files by their immediate containing package; an LLM enrichment pass
  can override that grouping per file where folder structure doesn't reflect
  real domains (see "Business modules" below).
- **LLM-written purpose summaries** for files/classes — a semantic layer added
  *after* the mechanical parse, via an agent (Claude Code, or any coding assistant)
  reading the actual files and posting summaries back through the API. This
  deliberately isn't a live API call baked into the backend, so running this adds
  no metered LLM cost of its own.
- **Doc-vs-code gap detection** — upload a high-level process doc and compare
  its claims against the whole codebase's real contents, surfacing real
  development gaps (a documented capability with no code behind it, or the
  reverse). Free by default (same non-live-API pattern as purpose summaries);
  an optional opt-in "Run automatic analysis" button will make one live,
  metered OpenAI API call to do the comparison itself instead, if
  `OPENAI_API_KEY` is set (see "Doc-vs-code gap detection" below for the
  tradeoff).

Everything is local: a FastAPI backend + SQLite, and a small dashboard UI. An
optional free Neo4j AuraDB instance can hold a browsable/queryable copy of each
run's graph. Nothing else is required to run this.

## Run it

```bash
git clone <this-repo> TestAtlas   # or just copy the folder
cd TestAtlas
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn server.app:app --reload --port 8123
```

Open **http://127.0.0.1:8123**. That's it — no API keys, no signup, no Neo4j
required to use the core features. `git` needs to be on `PATH` if you plan to
add an ADO or GitHub repo (not needed for "Local folder" repos).

## What you can do in the UI

- **Add repo** (top bar) — four source types:
  - **Local folder**: point at a checkout already on disk -- type/paste the
    path, or click **Browse…** to pick it in your OS's own folder dialog
    (macOS, Windows, or Linux with `zenity`/`kdialog`). A web page can never
    learn the absolute path of a folder it picked, so the *server* opens the
    dialog and returns the path, which only makes sense when TestAtlas is
    running on the same machine you're browsing from: the button is offered
    only to a same-machine (loopback) client, and never appears on a
    deployed instance like Fly, where you'd type the path of something
    already on that server instead.
  - **Upload**: for a deployed instance, where a "Local folder" path would
    point at the *server's* disk rather than your files. Pick a folder on
    your computer and the browser uploads its source files (in batches, since
    one request can't carry thousands); they're stored as a snapshot in the
    repo's own workspace directory and analyzed like any other checkout.
    Only source files the parsers read (Python, TypeScript, JavaScript) are
    ever sent or stored, with the ignore rules (`.git`, `node_modules`,
    virtualenvs, build output, vendored JS, ...) applied both in the browser
    and again on the server, so nothing else reaches the volume. It's a snapshot, not a live link: to pick up later
    changes, open Settings and upload the folder again (that replaces the
    old snapshot). The server treats filenames as untrusted input: paths are
    normalized and rejected if absolute, drive-lettered, or containing `..`,
    and there are caps on per-file size (2 MB), file count (20,000) and total
    size (200 MB) per repo, sized for a small machine. Deleting the repo
    deletes its uploaded files too. Not a substitute for GitHub/Azure DevOps
    sources when you want the graph to track a repo over time.
  - **GitHub**: paste a repo URL (`https://github.com/owner/repo`). Public repos
    need no token at all; private ones need a PAT with `repo` (classic) or
    `Contents: Read` (fine-grained) scope.
  - **Azure DevOps**: paste the repo's clone URL + a PAT with **Code (Read)**
    scope. "Test connection" validates either before you save.
  - Add as many repos as you want — each is analyzed and versioned independently.
- **Run analysis** — clones/pulls (ADO/GitHub), reads (local), or uses the uploaded snapshot of, the repo,
  parses every Python/TypeScript/JavaScript file, builds the graph, scores modules, computes findings,
  and stores a timestamped **run**. Syncs to Neo4j automatically if configured.
- **Overview** — node/edge/module/file counts, the actual module list (name,
  file/class/function counts, PageRank score — same data as Insights' module
  scores, surfaced here too so "Modules: 4" isn't just a bare number you have
  to go find the detail for elsewhere), and full run history.
- **Findings** — functions no static call reaches, files with no import
  edges, empty classes, unparsed files — filterable by category.
- **Graph** — an interactive, physics-based visualization for any past run.
  Nodes cluster and color by their owning business module. A **Neo4j (live)**
  toggle re-fetches the same run's graph straight from Neo4j instead of the
  local copy, with node size driven by the stored PageRank score.
- **Insights** — module scores (instant, local, no Neo4j needed) plus, if
  Neo4j is configured, the most-critical-nodes/bottleneck rankings and a
  direct link into Neo4j's own Browser.
- **Docs** — paste text, or upload as many actual documents as you want
  (`.docx`, `.pptx`, `.xlsx`, `.pdf`, `.md`, `.txt`) — high-level
  process/architecture docs covering the system overall (not one per
  module). "Add document(s)" accepts a multi-file selection in one go: each
  file becomes its own document, named from its filename, uploaded in one
  pass with a per-file success/failure summary. Non-text formats are
  extracted server-side (`server/doc_extract.py`, pure-Python libraries, no
  external service) into plain text on upload. In the **Gap Analysis** tab, "Get analysis context"
  bundles *every* one of the repo's documents together — as one corpus, not
  one at a time — with *every* module's real files/classes/functions/purpose
  summaries; hand that to an LLM session and ask it to compare the two, then
  paste the resulting findings back — shown there as summary stat cards, a
  per-category breakdown, and a filterable findings list. Surfaces real
  development gaps — a documented capability with no corresponding code, or
  code with no matching documentation. Comparing documents individually would
  miss that one doc covers what looks, in isolation, like a gap in another,
  so a repo's findings always come from a single combined pass over all of
  its documents. This can't fully self-drive for an anonymous visitor (same
  reason enrichment can't): the comparison needs an LLM actually reading both
  sides.
  - **Optional automatic mode**: if the deployment has `OPENAI_API_KEY` set,
    a "Run automatic analysis" button appears above the manual flow and makes
    one live call to OpenAI (`gpt-4o-mini`, `server/llm_gap_analysis.py`) to
    do the same combined comparison itself, replacing the repo's stored
    findings with the result. This is one of two parts of TestAtlas with a
    real, metered per-use cost (see "Test Cases" below for the other), both
    entirely opt-in — with no key configured this one's a no-op and the free
    manual flow above still works exactly as before.
    **Verified against production** (real API call, real response, findings
    stored correctly) — but the LLM's accuracy is not perfect: in testing it
    both invented a false "missing" finding for a class that was clearly
    present in the supplied codebase context, and missed a deliberately
    planted false requirement in the test document. Treat its findings as a
    fast first pass to review, not a substitute for the manual flow's
    human-in-the-loop check.
- **Test Cases** — **functional** test cases in Azure DevOps shape, generated
  **per module** with one live OpenAI call each (also gated on
  `OPENAI_API_KEY`, `server/llm_test_generation.py`). Each case has a title,
  description, priority (1–4), preconditions, and a **Steps grid where every
  step pairs an action with its own expected result** — the layout of an ADO
  test case — plus the code it exercises. They're user scenarios (or, for a
  library, how a consumer uses its public interface), *not* unit tests: an
  earlier version was given only function names and predictably wrote one case
  per function ("encode an empty string"). Three things make the difference:
  - **The model is told what functional means** — a scenario through visible
    behavior, often spanning several code units, never one case per function;
    an empty result for modules with no visible behavior (types, config,
    plumbing, test code).
  - **For web UIs it's given the real on-screen text.** `kg/ts_parser.py`
    extracts the button labels, headings, placeholders and messages from the
    JSX (including ternary/`&&` branches, but never `className`s, handlers or
    i18n keys), so steps say `click "Add To Cart"` instead of inventing
    wording, and are told not to invent labels that aren't listed.
  - **Coverage of the error messages is checked, not requested.** UI strings
    that read like errors/validation/empty-states/unavailable messages
    (`Out Of Stock`, `Please select an option`, `Your cart is empty.`) go into
    the prompt as a must-trigger list, are then verified against the returned
    steps *in the text*, and if any is uncovered exactly one follow-up call
    asks only for those. `covers` (the real functions/classes each scenario
    exercises) is validated against the module's actual code — invented names
    are dropped.

  Nothing is discarded silently: every case the validator rejects is reported
  with its reason (in the response and the UI toast), which matters — an early
  version required 2+ steps and quietly deleted 10 of 12 `gpt-4o` cases,
  because negative cases are naturally one step (one action, one error
  message); that rule was wrong and is gone. The per-module tab lists every real
  (non-test) module with a searchable Generate button (one metered call each,
  never in bulk); regenerating a module replaces only its cases; legacy
  unit-style cases still display alongside new ones until regenerated.
  Documents are optional (code alone is enough); count scales gently with the
  module's size (0.4/unit, floor 3, capped by what one response can hold — 25).

  **Model**: `gpt-4o-mini` by default (cheap); set `OPENAI_TEST_MODEL` (e.g.
  `fly secrets set OPENAI_TEST_MODEL=gpt-4o`) to use a stronger one for this
  feature only. Measured on a real storefront's cart module with the final
  pipeline, both models produced 9 well-formed cases, covered all three
  on-screen messages, and needed no follow-up call; `gpt-4o-mini` wrote fuller
  multi-step happy paths and `gpt-4o` more precise preconditions, and before the
  fixes above `gpt-4o-mini` in particular varied run to run (3 to 6 cases from
  the same input) — so judge on your own code rather than on this one sample.

  **Limits, honestly**: the model never sees function bodies, so it infers
  behavior from names, UI text and documents — treat the output as a reviewable
  first draft, not a QA suite; cases are generated per module, so a flow that
  spans several modules (browse → add to cart → check out) is only covered
  piecewise; and UI text is only extracted from TypeScript/JavaScript (JSX),
  so a Python web app's templates give the model names but not screen text.
  Reviewable records, not runnable code. No free manual fallback: with no
  `OPENAI_API_KEY`, the module list simply doesn't appear.
- **Compare runs** — pick a baseline and current run of the *same* repo:
  structural diff (nodes/edges added/removed/changed) and which findings are
  new/resolved/still open. Node ids are derived from stable dotted names
  (`file:pkg.mod`, `function:pkg.mod:func`), not random ids, so this is
  meaningful across runs instead of "everything looks new every time."

## TypeScript / JavaScript

Parsed with tree-sitter (`kg/ts_parser.py`) into the same File/Class/Function
+ IMPORTS/DEFINES/CALLS graph as Python, so everything downstream — module
scores, findings, gap analysis, per-module test generation, friendly module
names — works on it unchanged. Each File node carries its `language`, and the
Overview's Files card breaks a repo down by language.

What it understands:

- **Functions**: `function` declarations, arrow/function expressions bound to
  a top-level `const` (`export const Button = () => …` — the dominant form in
  React), those wrapped in `memo()`/`forwardRef()`/etc., `export default`
  (named or anonymous), and CommonJS `exports.x = …`.
- **Classes** and their methods, including constructors, getters/setters,
  static methods, and arrow-function class fields; `extends`/`implements` are
  recorded as bases.
- **Imports** — `import`, `export … from`, `require()`, dynamic `import()` —
  resolved against the repo: relative paths (with the `./x.js` → `x.ts` ESM
  convention and `index` files), tsconfig/jsconfig `paths` and `baseUrl`
  aliases like `@/components/x` (nearest config per file, `extends` followed,
  comments and trailing commas tolerated), and workspace packages in a
  monorepo (`@acme/core` → that package's source).
- **Calls** (best-effort, same spirit as the Python parser): direct calls,
  `this.method()`, calls through an import including through `index.ts`
  barrel re-exports (`export * from`, `export { x as y } from`),
  `Namespace.fn()`, static `Class.method()`, `new Class()` (→ its constructor),
  and **JSX tags** (`<Button/>` → the Button component — without this every
  React component would look unused).

Deliberately not modeled: interfaces, type aliases and enums (no behavior, and
as Class nodes they'd flood the "empty class" finding), `namespace` blocks,
object-literal methods, classes declared inside functions, and calls on a
variable of unknown type (`svc.run()`), or to inherited methods. Files with a
syntax error are still mined for whatever parsed (tree-sitter is
error-tolerant) rather than skipped whole. Skipped as noise: `.d.ts`
declaration files, `*.min.*` bundles, files over 1 MB, a `.js` that's the
compiled twin of a same-named `.ts`, framework build output (`.next`,
`.nuxt`, `dist`, `lcov-report` coverage output, …) and vendored *JavaScript* — vendored
*TypeScript* is kept, because it's source others import (tRPC does this). Test
files (`*.test.ts`, `*.spec.ts`, `__tests__/`) get the same "don't generate
tests for the tests" treatment Python's do.

Measured on real repos rather than assumed: relative imports resolve on
every code file that exists (the only misses were `.css`/`.json`/`.md` imports
and files a build step generates); class counts match an independent regex
count (the only difference being classes declared inside test functions);
TypeORM (3,600 files) parses in ~1.3 s at ~120 MB peak, since each file's
syntax tree is freed as soon as its symbols are extracted. `python -m unittest
discover -s tests -t .` runs the parser's regression tests. Python parsing
was verified byte-for-byte unchanged (same graph as before on FastAPI's
6,358 nodes / 7,578 edges and others). Re-running an existing repo that
contains JS/TS files (a Python project with a `docs/` or `static/` folder of
scripts, say) will now include those files in its graph.

## Business modules

"Module" in this app means a business capability (Accounts, Payments,
Statements), not a single source file — those are **File** nodes. Every
File/Class/Function gets a `domain` attribute:

- **Free default**: the file's immediate containing package. `app/accounts/models.py`
  and `app/accounts/views.py` both land in domain `app.accounts` — correct for
  most feature-organized codebases, at zero cost.
- **LLM override**: for layered architectures (`controllers/`, `services/`,
  `models/` split across a feature, so folder structure *doesn't* reflect the
  real domain), an enrichment entry can set `"domain": "Accounts"` on a File
  node directly — `POST /runs/{id}/enrichment` accepts it alongside `purpose`.
  Classes/functions defined in that file inherit the override automatically.
  `score_modules()` re-groups by this field the next time it's read, no
  separate rebuild step needed.

The graph schema itself doesn't change for this — it's a rollup computed from
`domain`, not new node types or edges, so it works the same locally and once
synced to Neo4j.

**Friendly display names**: `domain` itself stays a technical, stable
identifier (a dotted package path like `kg` or `src.itsdangerous.signer`) --
it's the key everything else keys off of (per-module test generation, doc
gap grouping, Compare Runs). A separate "Generate friendly module names"
button (Overview tab, gated on `OPENAI_API_KEY`, `server/llm_module_naming.py`)
makes one live OpenAI call naming every module in a repo at once (e.g. `kg`
-> "Knowledge Graph Engine") for a non-technical audience, purely as a
cosmetic label shown next to the raw name everywhere modules appear. Unlike
per-run domain overrides above (which the graph itself carries, and a fresh
`Run analysis` wipes), friendly names live in their own table keyed by
`(repo_id, module)` -- not tied to any run -- so they survive re-analysis
indefinitely without needing to be regenerated. Falls back to the raw
technical name anywhere a module hasn't been named yet.

## Architecture

```
static/                vanilla HTML/JS/CSS dashboard (no build step)
server/
  app.py                FastAPI routes
  doc_extract.py         extracts text from uploaded .docx/.pptx/.xlsx/.pdf/.md/.txt files
  folder_picker.py       opens the OS "choose a folder" dialog on the server's own machine
  uploads.py             stores a browser-uploaded folder (source files only) as a repo's source, with strict path/size validation
  llm_gap_analysis.py    optional: doc-vs-code comparison via a live OpenAI API
                          call (OPENAI_API_KEY) -- one of three metered features here
  llm_test_generation.py optional: designs test cases via a live OpenAI API
                          call (OPENAI_API_KEY, same key) -- another metered feature
  llm_module_naming.py   optional: friendly module display names via a live
                          OpenAI API call (OPENAI_API_KEY, same key) -- the
                          cheapest of the three (one call per repo, not one
                          per module)
  db.py                 SQLite: repos + runs
  crypto.py             Fernet encryption for stored PATs (data/secret.key, gitignored)
  auth.py                optional single-password gate (TESTATLAS_PASSWORD)
  runner.py              orchestrates one analysis run end-to-end
  diff.py                compares two runs of the same repo
ado/client.py            ADO REST (test-connection) + git clone/pull
github/client.py         GitHub REST (test-connection) + git clone/pull (PAT optional)
kg/
  code_model.py          language-neutral shapes every parser fills in (module/class/
                          function) + the shared pruned file walker
  python_ast_parser.py   mechanical parser: walks .py files via stdlib `ast`,
                          extracts files/classes/functions + resolved imports/calls
  ts_parser.py           the same for TypeScript/JavaScript via tree-sitter (see
                          "TypeScript / JavaScript" below)
  repo_parser.py         runs every language's parser over a checkout and merges
                          the results into one graph; defines what counts as source
  dev_graph_builder.py   builds the networkx.MultiDiGraph + business-module
                          scoring (rolls per-file scores up by `domain`)
  dev_queries.py         findings over that graph
  enrichment.py          LLM semantic layer: what needs a purpose summary
                          (and optionally a domain override), merged back onto the graph
  doc_gaps.py            LLM gap-analysis layer: bundles a doc + every
                          module's real contents for an agent to compare
  graph_io.py            shared load/save for a run's persisted graph.json
  visualize.py           pyvis interactive HTML (physics layout, domain-colored) + exports
  neo4j_sync.py          optional: syncs a run's graph into Neo4j, batched via UNWIND
  graph_intelligence.py  optional: reads PageRank/betweenness back out of Neo4j
```

The graph schema:

```
File -DEFINES-> Class -DEFINES-> Function (method)
File -DEFINES-> Function (top-level)
File -IMPORTS-> File
Function -CALLS-> Function
```

Every File/Class/Function also carries a `domain` attribute — see "Business
modules" above; it's what `score_modules()` groups by, not a separate node type.

## Neo4j (optional)

Set `NEO4J_URI` (`neo4j+s://...`), `NEO4J_USER`, `NEO4J_PASSWORD` to enable it —
every run then syncs automatically. A free [AuraDB](https://neo4j.com/product/auradb/)
instance works fine (no card required); the app never uses the Graph Data
Science plugin (not available on Aura Free/Professional), so PageRank/betweenness
are computed locally with networkx and written onto each node as plain
properties before syncing.

## Hosting this publicly

TestAtlas needs a **persistent process** — SQLite file, cloned repos on disk,
shells out to `git`. Rules out serverless/static hosts as-is; needs a small
always-on box (Fly.io, Railway, Render, a VPS).

### 1. Login gate

No login by default (fine for `localhost`). Set `TESTATLAS_PASSWORD` before
exposing this anywhere reachable — every request then requires a session
cookie, issued at `{BASE_PATH}/login`, until it expires (30 days). Single
shared password, not per-user accounts — enough to keep it off the open
internet, nothing more.

### 2. Reverse-proxied under a sub-path (e.g. `yoursite.com/testatlas`)

Set `BASE_PATH=/testatlas`. The app only answers under that prefix — whatever
sits in front of it just needs to forward the full path through unchanged
(no path-stripping/rewriting).

### 3. Deploying — locally, to sanity-check the image first

```bash
docker build -t testatlas .
docker run -p 8000:8000 \
  -e BASE_PATH=/testatlas \
  -e TESTATLAS_PASSWORD=<a real password> \
  -e NEO4J_URI=<optional> -e NEO4J_USER=<optional> -e NEO4J_PASSWORD=<optional> \
  -e OPENAI_API_KEY=<optional> \
  -v testatlas_persist:/app/persist \
  testatlas
# then: curl http://localhost:8000/testatlas/login
```

The volume is load-bearing: it holds the SQLite DB, run history, stored
graphs, the PAT encryption key, and cloned repos. Losing it means losing all
repo configs/run history.

### 4. Deploying — to Fly.io

A [fly.toml](fly.toml) is already in this repo. First time only:

```bash
brew install flyctl
fly auth signup   # or fly auth login
```

Then:

```bash
fly launch --copy-config --name <pick-a-unique-name> --no-deploy
fly volumes create testatlas_persist --size 2
fly secrets set TESTATLAS_PASSWORD="a real password"
fly secrets set NEO4J_URI="neo4j+s://..." NEO4J_USER="..." NEO4J_PASSWORD="..."  # optional
fly secrets set OPENAI_API_KEY="sk-..."  # optional -- enables automatic (paid) gap analysis
fly deploy
```

`fly.toml` scales to zero when idle (`min_machines_running = 0`) to stay
near-free — the tradeoff is a few seconds' cold-start delay on the first
request after idle time.

### Security notes

- PATs/tokens are encrypted at rest with a local Fernet key
  (`data/secret.key`, gitignored, auto-generated on first use) — fine for a
  single-user tool, not a substitute for a real secrets manager in a
  multi-tenant deployment.
- Keep PATs/tokens server-side only — the browser only ever talks to *this*
  backend's API.
- **"Local folder" repos only make sense where TestAtlas actually runs.**
  Hosted remotely, a `local_path` refers to a path on *that server*, not your
  laptop.
- `data/`, `workspace/` and `.venv/` are gitignored — don't commit them.

## Extending this

- **More languages**: Python and TypeScript/JavaScript are done; the pattern
  for the next one is the same as for TypeScript --
  [tree-sitter](https://tree-sitter.github.io/) (one library, per-language
  grammars) with a thin adapter (see `kg/ts_parser.py`) filling in the shared
  shapes in `kg/code_model.py` — not a second hand-written parser, and not an
  LLM doing the structural extraction (see below for why). Register it in
  `kg/repo_parser.py`, and the graph, scoring, findings, LLM features and UI
  all work for it with no further changes.
- **Why AST, not an LLM, for structure**: an LLM is the right tool for
  judgment calls ("what business module is this file part of") but the wrong tool for a large,
  exact, cross-referenced fact table (every import/call resolved against
  every file) — it's slower, non-deterministic (breaks Compare Runs' stable
  node ids), and can miss/hallucinate exactly the mechanical details that
  matter (aliased imports, indirect calls). The LLM's job here is strictly
  the semantic layer (`kg/enrichment.py`), on top of a mechanically exact graph.
- **SharePoint auto-ingest for docs**: the **Docs** tab (see below) takes
  manual paste/upload today, which gets most of the value for near-zero
  effort. Auto-pulling from SharePoint is the harder version of the same
  idea — needs an Azure AD app registration.
- **LLM-generated test cases**: built (see "Test Cases" above) as structured
  QA records covering the codebase broadly, prioritized by what the docs call
  out as important, and works from code alone when there are no documents.
  Not yet built:
  - Prioritizing specifically by (high PageRank/criticality) × (no existing
    test references it) -- the graph does parse test files today, but
    nothing yet links a test back to the function it exercises to compute
    "no existing test references it".
  - Generating actual runnable pytest stubs instead of QA-style records.
  - Surfacing doc-vs-code mismatches *as test cases* (e.g. "the document
    claims X, no matching code was found, so this can't be tested as
    written" -- a distinct category rather than silently skipping it, which
    is what happens today per the prompt's "must be traceable to something
    real" rule). This is deliberately NOT the same thing as a negative test
    case (invalid-input handling, already covered under `error_handling`) --
    it's a documentation-implementation gap, and today that's Gap Analysis's
    job exclusively (its `missing_implementation` category), kept separate
    from Test Cases on purpose rather than duplicated. Revisit if that
    separation turns out to be more friction than clarity in practice.
- **Automating the enrichment loop**: today a person (or an agent on their
  behalf) calls the enrichment API by hand. A "Generate purpose summaries"
  button that kicks off a background job doing the same thing would make
  this self-serve.
- **Cross-repo correlation**: node ids are per-repo today. Once you have 2+
  repos analyzed, detecting shared internal libraries is a `networkx.compose`
  away — prefix each repo's node ids with its repo id first.
- **Async/background runs**: `POST /repos/{id}/run` is synchronous today
  (fine for the repo sizes tested); swap for a `BackgroundTasks`/queue if
  repos get very large.
