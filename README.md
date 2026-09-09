# TestAtlas

Turns a codebase into a **knowledge graph** — File/Class/Function nodes,
IMPORTS/DEFINES/CALLS edges — using real AST parsing (zero cost, deterministic,
Python via the stdlib `ast` module today), then layers two things on top:

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

- **Add repo** (top bar) — three source types:
  - **Local folder**: point at a checkout already on disk.
  - **GitHub**: paste a repo URL (`https://github.com/owner/repo`). Public repos
    need no token at all; private ones need a PAT with `repo` (classic) or
    `Contents: Read` (fine-grained) scope.
  - **Azure DevOps**: paste the repo's clone URL + a PAT with **Code (Read)**
    scope. "Test connection" validates either before you save.
  - Add as many repos as you want — each is analyzed and versioned independently.
- **Run analysis** — clones/pulls (ADO/GitHub) or reads (local) the repo,
  parses every `.py` file, builds the graph, scores modules, computes findings,
  and stores a timestamped **run**. Syncs to Neo4j automatically if configured.
- **Overview** — node/edge/module/file counts and full run history.
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
  module). Non-text formats are extracted server-side
  (`server/doc_extract.py`, pure-Python libraries, no external service) into
  plain text on upload. In the **Gap Analysis** tab, "Get analysis context"
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
- **Test Cases** — a "Generate test cases" button (also gated on
  `OPENAI_API_KEY`, `server/llm_test_generation.py`) makes one live OpenAI
  call over the same graph+docs context gap analysis uses, and designs up to
  40 structured, QA-style test cases: title, preconditions, ordered steps,
  expected result, and which specific edge case it targets. Classified as
  `happy_path`, `edge_case` (boundary values, invalid input, timing/expiry),
  or `error_handling`, shown as summary stat cards plus a filterable list.
  These are reviewable records, not runnable code, and the model only ever
  sees names/purpose summaries (never real function bodies, which the graph
  doesn't carry) — treat them as a first draft to adapt, not a QA suite
  ready to run as-is. Unlike gap analysis, this has **no free manual
  fallback** today: with no `OPENAI_API_KEY` configured, the button simply
  doesn't appear.
- **Compare runs** — pick a baseline and current run of the *same* repo:
  structural diff (nodes/edges added/removed/changed) and which findings are
  new/resolved/still open. Node ids are derived from stable dotted names
  (`file:pkg.mod`, `function:pkg.mod:func`), not random ids, so this is
  meaningful across runs instead of "everything looks new every time."

## Business modules

"Module" in this app means a business capability (Accounts, Payments,
Statements), not a single `.py` file — those are **File** nodes. Every
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

## Architecture

```
static/                vanilla HTML/JS/CSS dashboard (no build step)
server/
  app.py                FastAPI routes
  doc_extract.py         extracts text from uploaded .docx/.pptx/.xlsx/.pdf/.md/.txt files
  llm_gap_analysis.py    optional: doc-vs-code comparison via a live OpenAI API
                          call (OPENAI_API_KEY) -- one of two metered features here
  llm_test_generation.py optional: designs test cases via a live OpenAI API
                          call (OPENAI_API_KEY, same key) -- the other metered feature
  db.py                 SQLite: repos + runs
  crypto.py             Fernet encryption for stored PATs (data/secret.key, gitignored)
  auth.py                optional single-password gate (TESTATLAS_PASSWORD)
  runner.py              orchestrates one analysis run end-to-end
  diff.py                compares two runs of the same repo
ado/client.py            ADO REST (test-connection) + git clone/pull
github/client.py         GitHub REST (test-connection) + git clone/pull (PAT optional)
kg/
  python_ast_parser.py   mechanical parser: walks .py files via stdlib `ast`,
                          extracts files/classes/functions + resolved imports/calls
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

- **More languages**: `kg/python_ast_parser.py` is Python-only today. The
  clean way to add another language is [tree-sitter](https://tree-sitter.github.io/)
  (one library, per-language grammars) with a thin adapter mapping its parse
  tree onto the same File/Class/Function schema — not a second hand-written
  parser, and not an LLM doing the structural extraction (see below for why).
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
  out as important. Not yet built: prioritizing specifically by (high
  PageRank/criticality) × (no existing test references it) -- the graph does
  parse test files today, but nothing yet links a test back to the function
  it exercises to compute "no existing test references it" -- and generating
  actual runnable pytest stubs instead of QA-style records.
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
