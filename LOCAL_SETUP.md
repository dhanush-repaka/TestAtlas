# Running TestAtlas on your own machine

This guide takes you from nothing to a working TestAtlas on a laptop or a
company server, with your own keys and your own data. It assumes no prior
knowledge of the project.

- [1. What you are installing](#1-what-you-are-installing)
- [2. Before you start](#2-before-you-start)
- [3. Install](#3-install)
- [4. Configure (`.env`)](#4-configure-env)
- [5. Start it](#5-start-it)
- [6. Your first ten minutes](#6-your-first-ten-minutes)
- [7. Adding repositories: the four sources](#7-adding-repositories-the-four-sources)
- [8. The AI features](#8-the-ai-features)
- [9. Your data: where it lives, backups, upgrades](#9-your-data-where-it-lives-backups-upgrades)
- [10. Sharing it with colleagues](#10-sharing-it-with-colleagues)
- [11. Running in Docker](#11-running-in-docker)
- [12. Troubleshooting](#12-troubleshooting)
- [13. Security checklist for a pilot](#13-security-checklist-for-a-pilot)

---

## 1. What you are installing

TestAtlas reads a codebase (Python, TypeScript, JavaScript), builds a
knowledge graph of it (files, classes, functions, who calls whom), groups it
into modules, and flags risks. On top of that, three **optional** features use
an OpenAI-compatible API:

- plain-English module names ("Shopping Cart" instead of `components.cart`),
- functional test cases in Azure DevOps style (title, steps, expected result),
- comparing your documents against the code to find gaps.

It is one Python process with a SQLite file. No database server, no Node.js
build, no Docker required. You open it in a web browser.

**What runs where.** Analysis, the graph, findings and everything you browse
happen on your machine. The only thing that ever leaves it is the AI features'
calls to the API you configure (section 8), plus `git` fetching repositories
from your GitHub / Azure DevOps host. With no API key set, nothing else leaves.

## 2. Before you start

| You need | Notes |
|---|---|
| **Python 3.13** | This is the version the project is developed and tested on. Other recent 3.x versions may work but are untested. Check with `python3 --version`. |
| **A terminal** | macOS Terminal, Linux shell, or Windows PowerShell. |
| **`git`** | Only if you will add repositories from GitHub or Azure DevOps. Not needed for local folders or uploads. Check with `git --version`. |
| **An OpenAI API key** | Only for the AI features. Everything else works without it. |
| **~1 GB free disk, 1 GB RAM** | More for very large repositories (thousands of files). The hosted demo runs medium repos on 512 MB. |

Tested on macOS. The steps for Windows and Linux below use the standard
equivalents but have not been run by the author; if something differs on your
system, section 12 lists the usual causes.

## 3. Install

### macOS / Linux

```bash
git clone https://github.com/dhanush-repaka/TestAtlas.git
cd TestAtlas
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
git clone https://github.com/dhanush-repaka/TestAtlas.git
cd TestAtlas
py -3.13 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

If PowerShell refuses to run scripts you do not need to activate the
virtual environment at all; the commands here call the programs inside `.venv`
directly.

### Check the install (optional, 5 seconds)

```bash
./.venv/bin/python3 -m unittest discover -s tests -t .
```

You should see `OK`. (On Windows: `.\.venv\Scripts\python -m unittest discover -s tests -t .`)

## 4. Configure (`.env`)

Settings and secrets live in a file called `.env` next to the app. It is
optional: with no `.env` the app runs with no login, no AI features, and
stores its data in the project folder.

```bash
cp .env.example .env       # Windows: copy .env.example .env
chmod 600 .env             # macOS/Linux only: readable by you alone
```

Then open `.env` in any editor. Every line is commented; the ones that matter:

| Setting | What it does | When to set it |
|---|---|---|
| `TESTATLAS_PASSWORD` | Login password for the whole app. | **Always**, unless only you can reach the machine. |
| `TESTATLAS_SECRET` | Signs the login cookie. | Recommended with a password. Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `OPENAI_API_KEY` | Turns on the three AI features. | If you want module names, test cases or gap analysis. |
| `OPENAI_TEST_MODEL` | Model used for test-case generation (default `gpt-4o-mini`). | To trade cost for quality. |
| `OPENAI_BASE_URL` | Send AI calls to an OpenAI-compatible endpoint you control instead of `api.openai.com`. | If your company requires an internal gateway. |
| `DATA_ROOT` | Folder for the database, run history, saved-token key and cloned repos. | To put data on a backed-up path. |
| `BASE_PATH` | Sub-path when behind a reverse proxy, e.g. `/testatlas`. | Only with a proxy (section 10). |
| `NEO4J_*` | Optional sync of the graph to Neo4j. | Only if you use Neo4j. |

**Rules of the file**

- One `KEY=value` per line. `#` starts a comment. If a value contains `#`,
  quote it: `TESTATLAS_PASSWORD="pass#word"`.
- **Real environment variables win.** If a setting is already set in your
  shell (or by Docker or a platform), the file does not override it.
- The app prints the *names* of what it loaded at startup, never the values.
  It warns if the file is readable by other users.
- `.env` is git-ignored and excluded from the Docker image. Never commit it or
  paste it into chat or tickets.
- Azure DevOps and GitHub tokens do **not** go here. You enter them per
  repository in the app and they are stored encrypted (section 9).

A minimal file for a solo trial:

```bash
OPENAI_API_KEY=sk-your-key-here
```

A typical file for a shared pilot server:

```bash
TESTATLAS_PASSWORD=a-long-passphrase
TESTATLAS_SECRET=paste-the-generated-random-string
OPENAI_API_KEY=sk-your-key-here
DATA_ROOT=/var/lib/testatlas
```

## 5. Start it

```bash
./.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8123
```

Windows: `.\.venv\Scripts\uvicorn server.app:app --host 127.0.0.1 --port 8123`

Open **http://127.0.0.1:8123** in your browser. If you set a password you will
see a login page first.

You should see in the terminal a line like
`TestAtlas: loaded 3 setting(s) from .../.env: TESTATLAS_PASSWORD, OPENAI_API_KEY, DATA_ROOT`
and then `Application startup complete`.

- **Stop:** press `Ctrl+C` in that terminal.
- **Another port:** change `--port 8123` (use this if 8123 is taken).
- **Keep it running after you close the terminal (macOS/Linux):**
  `nohup ./.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8123 > testatlas.log 2>&1 &`
  and stop it with `pkill -f "uvicorn server.app:app"`.
- **Restart after changing `.env`:** settings are read once at startup, so
  stop and start again.

`127.0.0.1` means only this machine can connect. To let colleagues reach it,
see section 10 first.

## 6. Your first ten minutes

1. **Add a repo.** Click **Add repo** (top right). Choose **Local folder**,
   click **Browse...** to pick a project folder on your machine (or paste its
   path), give it a name, and save.
2. **Run analysis.** Open the repo and click **Run analysis**. Small repos
   take seconds, large ones a minute or two. This produces a *run*, a
   timestamped snapshot you can compare with later ones.
3. **Look around.** **Overview** has counts and the module list; **Findings**
   lists risks; **Graph** is an interactive picture of the code; **Insights**
   shows the most critical and most connected parts.
4. **(Optional, needs a key) Name the modules.** Go to **Test Cases** and click
   **Name modules in plain English**. Each module gets a screen or feature name
   and a one-line description. Modules that only work behind the scenes are
   tucked into a collapsed "Behind the scenes" group.
5. **(Optional, needs a key) Generate test cases.** In **Test Cases**, click
   **Generate** next to a module. Each module is one small paid API call, run
   only when you click; nothing runs in bulk. You get functional cases with
   steps and an expected result for each step.
6. **Export.** In the Test Cases list, use **Export** to download **CSV
   (Azure DevOps)**, **Markdown** or **JSON**. It exports what the module and
   category filters currently show. The CSV follows Azure DevOps Test Plans'
   import layout and opens in Excel; try one small export into a test plan
   first, since ADO can be picky about columns depending on your process
   template.
7. **Re-run later.** After the code changes, click **Run analysis** again and
   use **Compare runs** to see what changed. Module names and generated test
   cases are kept between runs.

Adding **documents** (requirements, specs; `.md`, `.txt`, `.docx`, `.pptx`,
`.xlsx`, `.pdf`) in the **Docs** tab improves test-case priorities and enables
**Gap Analysis**.

## 7. Adding repositories: the four sources

| Source | Use when | What you provide |
|---|---|---|
| **Local folder** | The code is already on the machine running TestAtlas. | An absolute path, or **Browse...**. Reads it in place; re-run analysis to pick up changes. The Browse button appears only when you are on the same machine (it opens your OS's folder dialog; Linux needs `zenity` or `kdialog` and a desktop session). |
| **Upload** | TestAtlas runs on a server but the code is on your laptop. | Pick a folder in the browser; the source files are uploaded as a snapshot. Re-upload from **Settings** to refresh. Limits: 2 MB per file, 20,000 files, 200 MB per repo. |
| **GitHub** | Repo is on GitHub. | The URL. Public repos need no token. Private ones need a token with `repo` (classic) or `Contents: Read` (fine-grained) access. |
| **Azure DevOps** | Repo is on ADO. | The clone URL and a token with **Code (Read)** scope. |

Notes:

- Use **Test connection** before saving a GitHub or ADO repo.
- **Branch:** the form defaults to `main`. If the repo uses another default
  branch (for example `master`), set it in the branch field or the clone will
  fail.
- Tokens you enter are stored **encrypted** in your data folder and only ever
  used by the local server. They never go to the browser or to the AI API.
- Repositories are cloned into `workspace/` under your data folder.
- What is parsed: `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`. Vendored
  and generated folders (`node_modules`, `.next`, `dist`, virtualenvs, ...)
  are skipped automatically.

## 8. The AI features

Enabled only when `OPENAI_API_KEY` is set. Each call happens because you
clicked a button; the app never calls the API on its own.

**What is sent:** module, file and function names, the on-screen text found in
UI code (button labels, headings, error messages), file paths, and any
documents you added to the repo. **Not sent:** source-code bodies, your tokens,
or the `.env` file.

**Cost:** every "Generate" is one metered call (occasionally two or three when
the app asks the model to fix depth or missing error-message coverage). The
default model is `gpt-4o-mini`. Check your provider's pricing for current rates.

**Keeping it inside your network:** set `OPENAI_BASE_URL` to an
OpenAI-compatible gateway you operate. The library honours the variable; test
it against your own gateway before a pilot, as compatibility varies.

**Quality:** the default model is inexpensive and sometimes writes shallow or
awkward cases. Treat generated cases as a reviewed first draft. A stronger
model via `OPENAI_TEST_MODEL` may help but is not guaranteed to.

## 9. Your data: where it lives, backups, upgrades

Everything TestAtlas stores is under `DATA_ROOT` (default: the project folder):

| Path | Contents |
|---|---|
| `data/kg.db` | SQLite database: repos, runs, documents, findings, test cases, module names. |
| `data/runs/` | The stored graph of every run. |
| `data/secret.key` | The key that encrypts saved repo tokens. |
| `workspace/` | Cloned repositories and uploaded snapshots. |

- **Back up** the whole `data/` folder (stop the app first, or copy while idle).
  `workspace/` can be rebuilt by re-running analysis. Keep `secret.key` and
  `kg.db` together: without the key, saved tokens are unreadable. Equally,
  anyone holding both files can decrypt those tokens, so protect backups.
- **Start fresh:** stop the app and delete the `data/` and `workspace/` folders.
- **Upgrade:**
  ```bash
  git pull
  ./.venv/bin/pip install -r requirements.txt
  ```
  then restart. The database updates itself on startup; your data is kept.
  Read the release notes / git log first if you are mid-pilot.
- **Delete one repo** with the **Delete** button in its header; this also removes
  its runs, documents, findings, test cases and module names. (Cloned or uploaded
  files are cleaned up with it.)

## 10. Sharing it with colleagues

By default it listens on `127.0.0.1`, reachable only from the same machine.
To make it reachable on your network:

1. **Set `TESTATLAS_PASSWORD`** (and `TESTATLAS_SECRET`) in `.env`. Do not
   skip this step; without it anyone who can reach the port can use the app,
   read your code structure and spend your API budget.
2. Start with `--host 0.0.0.0`:
   `./.venv/bin/uvicorn server.app:app --host 0.0.0.0 --port 8123`
3. Colleagues open `http://<your-machine-name-or-ip>:8123`.

Things to know:

- It is **one shared password**, not per-user accounts. Everyone sees every
  repo. That is fine for a pilot, but be clear about it with your security team.
- There is no HTTPS on the built-in server. Put it behind your company's
  reverse proxy or load balancer for TLS. If the proxy serves it under a
  sub-path (e.g. `https://tools.company.com/testatlas`), set `BASE_PATH=/testatlas`
  and make the proxy forward the full path unchanged.
- **Browse...** only appears for someone on the same machine as the server.
  Colleagues use **Upload**, **GitHub** or **Azure DevOps** instead.

## 11. Running in Docker

Optional. Useful if the pilot machine is standardised on containers.

```bash
docker build -t testatlas .
docker run --rm -p 8123:8000 \
  --env-file .env \
  -v testatlas_persist:/app/persist \
  -e DATA_ROOT=/app/persist \
  testatlas
```

Open **http://127.0.0.1:8123**. The named volume holds your data; deleting it
deletes your repos and runs. The image contains `git`, so GitHub/ADO repos work
inside the container. Docker's `--env-file` does **not** strip quotes, so write
values unquoted in a file you pass to Docker (no `KEY="value"`). Local folders must be mounted into the container
(`-v /path/to/code:/code:ro`) and then added as `/code`. `.env` is never copied
into the image (it is excluded by `.dockerignore`); it is passed at run time.

This path has not been run by the author yet. If you try it, the two things
to watch are the volume mount and the port mapping.

## 12. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `python3: command not found` / wrong version | Install Python 3.13 (python.org, Homebrew `brew install python@3.13`, or your package manager). On Windows use `py -3.13`. |
| `pip install` fails on `tree-sitter` | No prebuilt package for your Python/OS combination, so pip tries to compile it. Use Python 3.13, or install a C compiler (Xcode command-line tools on macOS, build tools on Windows/Linux). |
| `Address already in use` | Another program uses that port. Choose another, e.g. `--port 8124`. |
| Browser shows **401** or a login page you did not expect | `TESTATLAS_PASSWORD` is set (in `.env` or your shell). Log in with it. To remove the login, unset it and restart. |
| Changed `.env` but nothing changed | Restart the app. Also check your shell does not already export that variable (real environment variables override the file). |
| No message about loading `.env` at startup | The file is not in the project root, is not named exactly `.env`, or has no valid `KEY=value` lines. You can point elsewhere with `TESTATLAS_ENV_FILE=/path/to/file`. |
| Module naming / test generation buttons are missing or say "not configured" | `OPENAI_API_KEY` is not set, or the app was not restarted after setting it. |
| "OpenAI API call failed: ... 401 / invalid key" | The key is wrong or revoked. Check for stray spaces or quotes. |
| "... 429 / rate limit / quota" | Your OpenAI account is out of credit or rate-limited. |
| GitHub/ADO clone fails, "Remote branch main not found" | The repo's default branch is different. Set the branch in the repo form (often `master`). |
| Clone fails / "git not found" | Install `git` and make sure it is on your `PATH`. |
| Private repo: authentication failed | Token expired or lacks read access to code. Create a new one with the scopes in section 7 and update it in **Settings**. |
| **Browse...** button missing | Expected when you connect from another machine, or on a headless Linux server. Paste the path instead, or use **Upload**. |
| Graph tab is slow or blank on a huge repo | Very large graphs are heavy for the browser. Use the Overview, Findings and Insights tabs instead. |
| Analysis runs out of memory | Analyse a sub-folder, or give the machine more RAM. |
| Module list shows raw names like `app.product.[handle]` | Run **Name modules in plain English** (needs the API key). |
| Home page or screen names look off for a non-Next.js project | The route-folder hints are tuned to Next.js and React. Names still come from the code and on-screen text but can be rougher elsewhere; you can regenerate them. |
| Tests fail after install | Post the last 20 lines of the output; usually a Python version or missing dependency. |

## 13. Security checklist for a pilot

- [ ] `TESTATLAS_PASSWORD` and `TESTATLAS_SECRET` set if anyone else can reach the machine.
- [ ] Access limited by your network (VPN / internal network); not exposed to the internet.
- [ ] TLS terminated at your reverse proxy if used over a network.
- [ ] `.env` is `chmod 600`, not committed, not in screenshots or chat.
- [ ] A dedicated, least-privilege OpenAI key with a spending limit (or your internal gateway via `OPENAI_BASE_URL`).
- [ ] Repository tokens are read-only (Code: Read / Contents: Read) and short-lived where possible.
- [ ] `DATA_ROOT` on an encrypted, backed-up disk; backups protected (they include the token key).
- [ ] Your data-handling team has seen section 8 (what the AI features send).
- [ ] Everyone knows it is a single shared login, and every user can see every repo.
