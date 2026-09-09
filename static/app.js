const API = "api"; // relative -- resolves against the page's <base href>, so this works unchanged whether served at "/" or under a reverse-proxied sub-path
let repos = [];
let activeRepoId = null;
let activeRuns = [];
let view = "dashboard"; // "dashboard" | "repo"

const $ = (sel, root = document) => root.querySelector(sel);
const $all = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ICON_LOCAL = `<svg class="ic" viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" stroke="currentColor" stroke-width="1.6"/></svg>`;
const ICON_ADO = `<svg class="ic" viewBox="0 0 24 24"><path d="M7 18a4 4 0 01-1-7.87A5 5 0 0116 8a4.5 4.5 0 011 8.9" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
const ICON_GITHUB = `<svg class="ic" viewBox="0 0 24 24"><path d="M12 2a10 10 0 00-3.16 19.5c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.46-1.15-1.11-1.46-1.11-1.46-.9-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.91.83.09-.65.35-1.08.63-1.33-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.5 9.5 0 015 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.74c0 .26.18.58.69.48A10 10 0 0012 2z" fill="currentColor"/></svg>`;

function sourceIcon(sourceType) {
  if (sourceType === "local") return ICON_LOCAL;
  if (sourceType === "github_git") return ICON_GITHUB;
  return ICON_ADO;
}
const ICON_ALERT = `<svg class="ic" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.86L1.8 18a1.5 1.5 0 001.3 2.25h17.9A1.5 1.5 0 0022.3 18L13.7 3.86a1.5 1.5 0 00-2.6 0z" stroke="currentColor" stroke-width="1.6"/></svg>`;
const ICON_CHECK = `<svg class="ic" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_CRITICAL = `<svg class="ic" viewBox="0 0 24 24"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`;

// Keep in sync with kg/visualize.py COLORS
const NODE_COLORS = {
  Feature: "#8e44ad", Scenario: "#2980b9", Step: "#7f8c8d", StepDefinition: "#16a085",
  Fixture: "#d35400", PageClass: "#c0392b", Method: "#27ae60", Locator: "#f39c12",
  TestData: "#95a5a6", TestCase: "#3498db",
};

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (kind ? " " + kind : "");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 4500);
}

async function api(path, opts = {}) {
  const resp = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let msg = resp.statusText;
    try { msg = (await resp.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTimeAgo(iso) {
  if (!iso) return "never run";
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

// The "Knowledge Graph Index" is a simple, transparent quality score derived
// straight from the latest run's findings: start at 100, lose 5 points per
// open finding. It's not a scientific metric -- it's a fast, at-a-glance
// signal for "does this repo's graph look healthy", clickable through to the
// Findings tab for the real detail.
function kgIndex(latestRun) {
  if (!latestRun || latestRun.status !== "success") return null;
  const findings = latestRun.finding_count ?? 0;
  return Math.max(0, Math.min(100, 100 - findings * 5));
}
function scoreBucket(score) {
  if (score === null) return "none";
  if (score >= 80) return "ok";
  if (score >= 50) return "warn";
  return "danger";
}
function kgRingSvg(score) {
  const bucket = scoreBucket(score);
  const r = 18, c = 2 * Math.PI * r;
  const frac = score === null ? 0 : score / 100;
  return `
    <div class="kg-ring score-${bucket}" title="${score === null ? "No successful run yet" : `Knowledge Graph Index: ${score}/100 (100 − 5 pts per open finding)`}">
      <svg viewBox="0 0 44 44">
        <circle class="track" cx="22" cy="22" r="${r}"></circle>
        <circle class="progress" cx="22" cy="22" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - frac)}"></circle>
      </svg>
      <div class="ring-value">${score === null ? "—" : score}</div>
    </div>`;
}

// --------------------------------------------------------------------------- dashboard (tile grid)

async function loadRepos() {
  repos = await api("/repos");
  if (view === "dashboard") renderDashboard();
}

async function renderDashboard() {
  const grid = $("#repoTiles");
  if (!repos.length) {
    grid.innerHTML = `<div class="add-tile" id="dashAddTile"><span class="add-icon"><svg class="ic" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></span><span>Add your first repo</span></div>`;
    $("#dashAddTile").addEventListener("click", () => openRepoModal());
    return;
  }
  // Show tiles immediately with skeleton stats, then fill in once run history loads.
  grid.innerHTML = repos.map((r) => repoTileHtml(r, null)).join("") + addTileHtml();
  wireTiles();

  const runsByRepo = await Promise.all(repos.map((r) => api(`/repos/${r.id}/runs`).catch(() => [])));
  if (view !== "dashboard") return; // user navigated away while this was in flight
  grid.innerHTML = repos.map((r, i) => repoTileHtml(r, runsByRepo[i][0] || null)).join("") + addTileHtml();
  wireTiles();
}

function addTileHtml() {
  return `<div class="add-tile" id="dashAddTile"><span class="add-icon"><svg class="ic" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></span><span>Add repo</span></div>`;
}

function wireTiles() {
  $all(".repo-tile").forEach((el) => el.addEventListener("click", () => showRepoDetail(el.dataset.id)));
  const addTile = $("#dashAddTile");
  if (addTile) addTile.addEventListener("click", () => openRepoModal());
}

function repoTileHtml(repo, latestRun) {
  const score = kgIndex(latestRun);
  const bucket = scoreBucket(score);
  const accentColor = { ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)", none: "var(--border)" }[bucket];
  const meta =
    repo.source_type === "local" ? repo.local_path || ""
    : repo.source_type === "github_git" ? `${repo.github_owner}/${repo.github_repo}`
    : `${repo.ado_org}/${repo.ado_project}/${repo.ado_repo}`;
  const stats = latestRun && latestRun.status === "success" ? latestRun.stats || {} : null;
  const moduleCount = stats?.module_scores?.length ?? "—";
  const topModule = stats?.module_scores?.length ? stats.module_scores[0] : null;
  return `
    <div class="repo-tile" data-id="${repo.id}">
      <div class="tile-accent" style="background:${accentColor}"></div>
      <div class="tile-top">
        <span class="tile-icon">${sourceIcon(repo.source_type)}</span>
        ${kgRingSvg(score)}
      </div>
      <h3>${escapeHtml(repo.name)}</h3>
      <div class="tile-meta">${escapeHtml(meta)}</div>
      <div class="tile-stats">
        ${stats ? `<span><b>${stats.nodes ?? "—"}</b> nodes</span><span><b>${moduleCount}</b> modules</span><span><b>${latestRun.finding_count ?? "—"}</b> findings</span>` : `<span class="muted">${latestRun ? "Last run failed" : "Not analyzed yet"}</span>`}
      </div>
      ${topModule ? `<div class="tile-top-module" title="Highest PageRank module in this run — most of the codebase structurally depends on it">${ICON_CRITICAL}<b>${escapeHtml(topModule.module)}</b><span class="muted">most critical</span></div>` : ""}
      <div class="tile-footer">
        ${latestRun ? `<span class="status-pill status-${latestRun.status}">${latestRun.status}</span>` : `<span class="muted">—</span>`}
        <span class="muted">${fmtTimeAgo(latestRun && latestRun.started_at)}</span>
      </div>
    </div>`;
}

// --------------------------------------------------------------------------- views

function showDashboard() {
  view = "dashboard";
  activeRepoId = null;
  $("#dashboardView").hidden = false;
  $("#repoView").hidden = true;
  renderDashboard();
}

async function showRepoDetail(id) {
  view = "repo";
  activeRepoId = id;
  $("#dashboardView").hidden = true;
  $("#repoView").hidden = false;
  const repo = repos.find((r) => r.id === id);
  $("#repoTitle").textContent = repo.name;
  $("#repoSourceChip").innerHTML = sourceIcon(repo.source_type);
  $("#repoSubtitle").textContent =
    repo.source_type === "local" ? `Local folder · ${repo.local_path}`
    : repo.source_type === "github_git" ? `GitHub · ${repo.github_owner}/${repo.github_repo} (${repo.github_branch})`
    : `Azure DevOps · ${repo.ado_org}/${repo.ado_project}/${repo.ado_repo} (${repo.ado_branch})`;
  await loadRuns();
  await loadDocsPanel();
  await loadGapsPanel();
  await loadTestCasesPanel();
}

async function loadRuns() {
  const repoId = activeRepoId;
  const runs = await api(`/repos/${repoId}/runs`);
  if (repoId !== activeRepoId) return; // stale response from a repo we've since navigated away from
  activeRuns = runs;
  renderOverview();
  populateRunSelectors();
}

function renderOverview() {
  const latest = activeRuns[0];
  const cards = $("#statCards");
  const moduleList = $("#overviewModuleList");
  if (!latest || latest.status !== "success") {
    cards.innerHTML = `<div class="stat-card"><div class="num">—</div><div class="lbl">No successful run yet</div></div>`;
    moduleList.innerHTML = "";
  } else {
    const s = latest.stats || {};
    const byType = s.by_node_type || {};
    cards.innerHTML = [
      statCard(s.nodes ?? "—", "Total nodes"),
      statCard(s.edges ?? "—", "Total edges"),
      statCard(s.module_scores?.length ?? 0, "Modules"),
      statCard(byType.File ?? 0, "Files"),
      statCard(latest.finding_count ?? 0, "Findings"),
    ].join("");
    // The "Modules" card above is just a count -- list what they actually
    // are right here too, not only buried in the Insights tab.
    moduleList.innerHTML = s.module_scores?.length ? renderModuleScores(s.module_scores, s.skipped_files || []) : "";
  }

  const tbody = $("#runTable tbody");
  tbody.innerHTML = activeRuns
    .map(
      (r) => `
      <tr>
        <td>${fmtDate(r.started_at)}</td>
        <td><span class="status-pill status-${r.status}">${r.status}</span></td>
        <td>${r.stats?.nodes ?? "—"}</td>
        <td>${r.stats?.edges ?? "—"}</td>
        <td>${r.finding_count ?? "—"}</td>
        <td>${r.status === "failed" ? `<span class="small muted" title="${escapeHtml(r.error || "")}">error ⓘ</span>` : ""}</td>
      </tr>`
    )
    .join("") || `<tr><td colspan="6" class="muted">No runs yet.</td></tr>`;
}

function statCard(num, label) {
  return `<div class="stat-card"><div class="num">${num}</div><div class="lbl">${label}</div></div>`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

function populateRunSelectors() {
  const successRuns = activeRuns.filter((r) => r.status === "success");
  const opts = successRuns.map((r) => `<option value="${r.id}">${fmtDate(r.started_at)}</option>`).join("");
  for (const sel of [$("#findingsRunSelect"), $("#graphRunSelect"), $("#insightsRunSelect"), $("#compareBaseline"), $("#compareCurrent")]) {
    sel.innerHTML = opts || "<option value=''>No successful runs</option>";
  }
  if (successRuns.length > 1) $("#compareBaseline").selectedIndex = 1; // default: baseline = second-most-recent
  loadFindingsPanel();
  loadGraphPanel();
  loadInsightsPanel();
}

// --------------------------------------------------------------------------- findings

async function loadFindingsPanel() {
  const runId = $("#findingsRunSelect").value;
  const tbody = $("#findingsTable tbody");
  if (!runId) { tbody.innerHTML = `<tr><td colspan="3" class="muted">No successful run to show.</td></tr>`; return; }
  const run = await api(`/runs/${runId}`);
  const findings = run.findings || [];
  const categories = [...new Set(findings.map((f) => f.category))];
  const catSel = $("#findingsCategoryFilter");
  const currentFilter = catSel.value;
  catSel.innerHTML = `<option value="">All categories (${findings.length})</option>` +
    categories.map((c) => `<option value="${c}">${c.replaceAll("_", " ")}</option>`).join("");
  catSel.value = categories.includes(currentFilter) ? currentFilter : "";

  renderFindingsTable(findings);
}

function renderFindingsTable(findings) {
  const filter = $("#findingsCategoryFilter").value;
  const rows = filter ? findings.filter((f) => f.category === filter) : findings;
  const tbody = $("#findingsTable tbody");
  tbody.innerHTML =
    rows
      .map(
        (f) => `
      <tr>
        <td><span class="finding-cat cat-${f.category}">${f.category.replaceAll("_", " ")}</span></td>
        <td class="small muted">${escapeHtml(f.node_id)}</td>
        <td>${escapeHtml(f.label)}</td>
      </tr>`
      )
      .join("") || `<tr><td colspan="3" class="muted">No findings in this category 🎉</td></tr>`;
}

// --------------------------------------------------------------------------- graph

let graphSource = "local";

function setGraphSource(value) {
  graphSource = value;
  $all("#graphSourceSegmented .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  $("#graphSourceHint").hidden = value !== "neo4j";
  loadGraphPanel();
}

function loadGraphPanel() {
  const runId = $("#graphRunSelect").value;
  const frame = $("#graphFrame");
  const endpoint = graphSource === "neo4j" ? "neo4j-graph.html" : "graph.html";
  frame.src = runId ? `${API}/runs/${runId}/${endpoint}` : "about:blank";
  $("#graphLegend").innerHTML = Object.entries(NODE_COLORS)
    .map(([type, color]) => `<span class="item"><span class="swatch" style="background:${color}"></span>${type}</span>`)
    .join("");
}

// --------------------------------------------------------------------------- insights (Neo4j / GDS)

let _insightsPollTimer = null;

async function loadInsightsPanel() {
  clearTimeout(_insightsPollTimer);
  const runId = $("#insightsRunSelect").value;
  const out = $("#insightsResult");
  if (!runId) { out.innerHTML = `<p class="muted small">No successful run to show.</p>`; return; }
  out.innerHTML = `<p class="muted small">Loading…</p>`;
  try {
    const [data, run] = await Promise.all([api(`/runs/${runId}/insights`), api(`/runs/${runId}`)]);
    const moduleScores = run?.stats?.module_scores;
    out.innerHTML = (moduleScores ? renderModuleScores(moduleScores, run.stats.skipped_files || []) : "") + renderInsights(data);
    if (data.configured && data.warming_up) {
      _insightsPollTimer = setTimeout(() => {
        if ($("#insightsRunSelect").value === runId) loadInsightsPanel();
      }, 5000);
    }
  } catch (e) {
    out.innerHTML = `<p class="muted small">${escapeHtml(e.message)}</p>`;
  }
}

function renderModuleScores(scores, skippedFiles) {
  if (!scores.length) return "";
  const maxRank = Math.max(...scores.map((s) => s.pagerank), 1e-9);
  const rows = scores
    .slice(0, 15)
    .map(
      (s, idx) => `
      <div class="rank-row">
        <span class="rank-num">${idx + 1}</span>
        <div class="rank-body">
          <div class="rank-label">${escapeHtml(s.module)}${s.isolated ? ` <span class="finding-cat cat-dead_locator">not yet connected</span>` : ""}</div>
          <div class="small muted">${s.file_count} file${s.file_count === 1 ? "" : "s"} · ${s.class_count} classes · ${s.function_count} functions · imported by ${s.imported_by_count}</div>
          ${s.purpose ? `<div class="small module-purpose">${escapeHtml(s.purpose)}</div>` : ""}
          <div class="rank-bar-track"><div class="rank-bar" style="width:${Math.max(4, (s.pagerank / maxRank) * 100)}%"></div></div>
        </div>
        <span class="rank-score">${s.pagerank.toFixed(3)}</span>
      </div>`
    )
    .join("");
  return `<div class="result-block">
    <h3>Module scores <span class="muted small" style="font-weight:400;">— PageRank over the dev-code graph</span></h3>
    <p class="muted small">Higher = more of the codebase structurally depends on this module. Computed instantly from this run's own graph — no Neo4j required.</p>
    <div class="rank-list">${rows}</div>
    ${skippedFiles.length ? `<p class="muted small">${skippedFiles.length} file(s) had syntax errors and were skipped: ${skippedFiles.map(escapeHtml).join(", ")}</p>` : ""}
  </div>`;
}

function renderInsights(data) {
  if (!data.configured) {
    return `<div class="result-block">
      <p class="muted">Neo4j isn't configured for this deployment, so there's nothing to show here yet.</p>
      <p class="muted small">Set <code>NEO4J_URI</code> (+ <code>NEO4J_USER</code> / <code>NEO4J_PASSWORD</code>) and re-run this repo's analysis — every run syncs its graph to Neo4j automatically once configured.</p>
    </div>`;
  }

  if (data.warming_up) {
    return `<div class="result-block">
      <div class="rank-row" style="justify-content:center; gap:10px; padding:16px;">
        <svg class="ic spin-icon" viewBox="0 0 24 24" style="width:16px;height:16px;"><path d="M12 3a9 9 0 100 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
        <span class="muted small">Waking up the graph database — this can take up to a minute the first time. Retrying automatically…</span>
      </div>
    </div>`;
  }

  const rankedList = (items, emptyMsg) => {
    if (!items.length) return `<p class="muted small">${emptyMsg}</p>`;
    const max = Math.max(...items.map((i) => i.score), 1e-9);
    return `<div class="rank-list">${items
      .map(
        (i, idx) => `
      <div class="rank-row">
        <span class="rank-num">${idx + 1}</span>
        <div class="rank-body">
          <div class="rank-label">${escapeHtml(i.label)} <span class="finding-cat cat-dead_locator">${escapeHtml(i.type)}</span></div>
          <div class="rank-bar-track"><div class="rank-bar" style="width:${Math.max(4, (i.score / max) * 100)}%"></div></div>
        </div>
        <span class="rank-score">${i.score.toFixed(2)}</span>
      </div>`
      )
      .join("")}</div>`;
  };

  return `<div class="result-block">
    ${data.browser_url ? `<a class="btn" href="${escapeHtml(data.browser_url)}" target="_blank" rel="noopener" style="display:inline-flex;margin-bottom:16px;">Open in Neo4j Browser ↗</a>` : ""}

    <h3>Most critical nodes <span class="muted small" style="font-weight:400;">— PageRank</span></h3>
    <p class="muted small">Nodes many other nodes structurally depend on, directly or transitively. High score = high blast radius if this one breaks.</p>
    ${rankedList(data.most_critical, "Nothing scored highly enough to surface — a small or shallow graph.")}

    <h3>Bottlenecks <span class="muted small" style="font-weight:400;">— Betweenness centrality</span></h3>
    <p class="muted small">Nodes sitting on the most paths between other nodes — a chokepoint even if it isn't the most "popular" node.</p>
    ${rankedList(data.bottlenecks, "No real bottlenecks found — the graph's dependency paths are well distributed.")}
  </div>`;
}

// --------------------------------------------------------------------------- compare

async function runCompare() {
  const baseline = $("#compareBaseline").value;
  const current = $("#compareCurrent").value;
  const out = $("#compareResult");
  if (!baseline || !current) { toast("Pick both a baseline and a current run", "error"); return; }
  if (baseline === current) { toast("Pick two different runs", "error"); return; }
  try {
    const d = await api(`/repos/${activeRepoId}/compare?baseline=${baseline}&current=${current}`);
    out.innerHTML = renderDiff(d);
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderDiff(d) {
  const list = (items, empty) =>
    items.length ? `<ul>${items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>` : `<p class="muted small">${empty}</p>`;

  const noChanges = !d.nodes_added.length && !d.nodes_removed.length && !d.nodes_changed.length && !d.edges_added.length && !d.edges_removed.length;

  return `<div class="result-block">
    <h3>Findings</h3>
    <div class="card-grid">
      ${statCard(d.findings_new.length, "New findings")}
      ${statCard(d.findings_resolved.length, "Resolved findings")}
      ${statCard(d.findings_persisting.length, "Still open")}
    </div>
    ${d.findings_new.length ? `<h4 class="pill-remove">New</h4>${list(d.findings_new.map((f) => `${f.category}: ${f.label}`), "")}` : ""}
    ${d.findings_resolved.length ? `<h4 class="pill-add">Resolved</h4>${list(d.findings_resolved.map((f) => `${f.category}: ${f.label}`), "")}` : ""}

    <h3>Graph structure</h3>
    <div class="card-grid">
      ${statCard(d.nodes_added.length, "Nodes added")}
      ${statCard(d.nodes_removed.length, "Nodes removed")}
      ${statCard(d.nodes_changed.length, "Nodes changed")}
      ${statCard(d.edges_added.length, "Edges added")}
      ${statCard(d.edges_removed.length, "Edges removed")}
    </div>
    ${d.nodes_added.length ? `<h4 class="pill-add">Added</h4>${list(d.nodes_added.map((n) => `${n.type}: ${n.label}`), "")}` : ""}
    ${d.nodes_removed.length ? `<h4 class="pill-remove">Removed</h4>${list(d.nodes_removed.map((n) => `${n.type}: ${n.label}`), "")}` : ""}
    ${d.nodes_changed.length ? `<h4 class="pill-change">Changed</h4>${list(d.nodes_changed.map((n) => `${n.type}: ${n.label} — ${Object.keys(n.changes).join(", ")}`), "")}` : ""}
    ${noChanges ? `<p class="muted small">No structural changes between these two runs.</p>` : ""}
  </div>`;
}

// --------------------------------------------------------------------------- tabs

function initTabs() {
  $all(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      $all(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      $all(".tab-panel").forEach((p) => (p.hidden = true));
      $(`#panel-${tab.dataset.tab}`).hidden = false;
      if (tab.dataset.tab !== "insights") clearTimeout(_insightsPollTimer);
    })
  );
}

// --------------------------------------------------------------------------- actions

async function runAnalysis() {
  const btn = $("#runBtn");
  const label = $("#runBtnLabel");
  $all(".header-actions .btn").forEach((b) => (b.disabled = true));
  $(".run-icon", btn).classList.add("is-hidden");
  $(".spin-icon", btn).classList.remove("is-hidden");
  label.textContent = "Running…";
  toast("Running analysis…");
  try {
    const run = await api(`/repos/${activeRepoId}/run`, { method: "POST" });
    if (run.status === "success") toast("Run complete", "ok");
    else toast("Run failed: " + (run.error || "unknown error").split("\n")[0], "error");
    await loadRuns();
  } catch (e) {
    toast("Run failed: " + e.message, "error");
    await loadRuns();
  } finally {
    $all(".header-actions .btn").forEach((b) => (b.disabled = false));
    $(".run-icon", btn).classList.remove("is-hidden");
    $(".spin-icon", btn).classList.add("is-hidden");
    label.textContent = "Run analysis";
  }
}

async function testConnection() {
  try {
    const r = await api(`/repos/${activeRepoId}/test-connection`, { method: "POST" });
    toast(r.message, r.ok ? "ok" : "error");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function deleteRepo() {
  if (!confirm("Delete this repo and all its run history? This cannot be undone.")) return;
  await api(`/repos/${activeRepoId}`, { method: "DELETE" });
  await loadRepos();
  showDashboard();
}

// --------------------------------------------------------------------------- modal
//
// One modal serves both "Add repo" (editingRepoId === null) and the repo's
// "Settings" button (editingRepoId set) -- same fields, prefilled from the
// repo's current config, PUT instead of POST on submit. The PAT field is
// always left blank when editing (the server never returns a stored PAT);
// leaving it blank on save keeps the existing one.

let editingRepoId = null;

function openRepoModal(repo = null) {
  $("#repoForm").reset();
  $("#adoTestResult").textContent = "";
  $("#githubTestResult").textContent = "";
  editingRepoId = repo ? repo.id : null;
  $("#repoModalTitle").textContent = repo ? "Repo settings" : "Add repo";
  $("#repoSubmitBtn").textContent = repo ? "Save changes" : "Save repo";

  if (repo) {
    $("#repoNameInput").value = repo.name;
    setSourceType(repo.source_type);
    if (repo.source_type === "local") {
      $("#repoForm [name=local_path]").value = repo.local_path || "";
    } else if (repo.source_type === "github_git") {
      $("#githubUrlInput").value = `https://github.com/${repo.github_owner}/${repo.github_repo}`;
      $("#repoForm [name=github_branch]").value = repo.github_branch || "main";
      $("#githubPatInput").placeholder = repo.has_github_pat ? "token already set — leave blank to keep it" : "only needed for private repos";
    } else {
      $("#adoUrlInput").value = `https://dev.azure.com/${repo.ado_org}/${repo.ado_project}/_git/${repo.ado_repo}`;
      $("#repoForm [name=ado_branch]").value = repo.ado_branch || "main";
      $("#adoPatInput").placeholder = repo.has_pat ? "PAT already set — leave blank to keep it" : "stored encrypted, never shown again";
    }
  } else {
    setSourceType("local");
    $("#adoPatInput").placeholder = "stored encrypted, never shown again";
    $("#githubPatInput").placeholder = "only needed for private repos";
  }
  $("#repoModalBackdrop").hidden = false;
}
function closeRepoModal() {
  $("#repoModalBackdrop").hidden = true;
}
function setSourceType(value) {
  $("#sourceTypeInput").value = value;
  $all("#sourceSegmented .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  $("#localFields").hidden = value !== "local";
  $("#adoFields").hidden = value !== "ado_git";
  $("#githubFields").hidden = value !== "github_git";
}

async function submitRepoForm(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = {
    name: fd.get("name"),
    source_type: fd.get("source_type"),
  };
  if (payload.source_type === "local") {
    payload.local_path = fd.get("local_path");
  } else if (payload.source_type === "github_git") {
    payload.github_url = fd.get("github_url");
    payload.github_branch = fd.get("github_branch") || "main";
    const ghPat = fd.get("github_pat");
    if (ghPat) payload.github_pat = ghPat; // omit entirely when blank so an edit doesn't wipe a stored token
  } else {
    payload.ado_url = fd.get("ado_url");
    payload.ado_branch = fd.get("ado_branch") || "main";
    const pat = fd.get("ado_pat");
    if (pat) payload.ado_pat = pat; // omit entirely when blank so an edit doesn't wipe the stored PAT
    else if (!editingRepoId) payload.ado_pat = pat; // creating: let the server 400 with a clear message
  }
  try {
    const repo = editingRepoId
      ? await api(`/repos/${editingRepoId}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/repos", { method: "POST", body: JSON.stringify(payload) });
    closeRepoModal();
    await loadRepos();
    showRepoDetail(repo.id);
    toast(editingRepoId ? "Repo settings saved" : "Repo added", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function testAdoFromModal() {
  const form = $("#repoForm");
  const fd = new FormData(form);
  const url = fd.get("ado_url"), pat = fd.get("ado_pat");
  const resultEl = $("#adoTestResult");
  if (!url || !pat) { resultEl.textContent = "Fill in the repository URL and PAT first."; return; }
  resultEl.textContent = "Testing…";
  try {
    const r = await api("/ado/test-connection", { method: "POST", body: JSON.stringify({ ado_url: url, ado_pat: pat }) });
    resultEl.textContent = r.message;
    resultEl.className = "small " + (r.ok ? "pill-add" : "pill-remove");
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "small pill-remove";
  }
}

async function testGithubFromModal() {
  const form = $("#repoForm");
  const fd = new FormData(form);
  const url = fd.get("github_url"), pat = fd.get("github_pat");
  const resultEl = $("#githubTestResult");
  if (!url) { resultEl.textContent = "Fill in the repository URL first (a token is only needed for private repos)."; return; }
  resultEl.textContent = "Testing…";
  try {
    const r = await api("/github/test-connection", { method: "POST", body: JSON.stringify({ github_url: url, github_pat: pat || null }) });
    resultEl.textContent = r.message;
    resultEl.className = "small " + (r.ok ? "pill-add" : "pill-remove");
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "small pill-remove";
  }
}

// --------------------------------------------------------------------------- theme

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $(".theme-sun").classList.toggle("is-hidden", theme === "light");
  $(".theme-moon").classList.toggle("is-hidden", theme !== "light");
  try { localStorage.setItem("ta-theme", theme); } catch (e) {}
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

// --------------------------------------------------------------------------- documents & gap analysis

let activeDocuments = [];
let editingDocId = null;

async function loadDocsPanel() {
  const repoId = activeRepoId;
  const docs = await api(`/repos/${repoId}/documents`).catch(() => []);
  if (repoId !== activeRepoId) return; // stale response from a repo we've since navigated away from
  activeDocuments = docs;
  renderDocsList();
}

// --------------------------------------------------------------------------- gap analysis (separate tab)

let activeGapFindings = [];

let autoGapAnalysisAvailable = null; // null = not checked yet

async function loadGapsPanel() {
  const repoId = activeRepoId;
  if (!activeDocuments.length) activeDocuments = await api(`/repos/${repoId}/documents`).catch(() => []);
  if (repoId !== activeRepoId) return; // stale response from a repo we've since navigated away from
  renderGapDocsIncluded();

  if (autoGapAnalysisAvailable === null) {
    autoGapAnalysisAvailable = await api("/gap-analysis/config")
      .then((c) => c.automatic_available)
      .catch(() => false);
    if (repoId !== activeRepoId) return;
    $("#runAutoGapBtn").classList.toggle("is-hidden", !autoGapAnalysisAvailable);
    $("#autoGapHint").classList.toggle("is-hidden", !autoGapAnalysisAvailable);
  }

  const findings = await api(`/repos/${repoId}/gap-findings`).catch(() => []);
  if (repoId !== activeRepoId) return; // don't clobber a repo we've since navigated to with this one's results
  activeGapFindings = findings;
  renderGapSummary();
  renderGapFindingsList();
}

async function runAutoGapAnalysis() {
  if (!activeDocuments.length) { toast("Add at least one document first", "error"); return; }
  const btn = $("#runAutoGapBtn");
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Analyzing…";
  try {
    const result = await api(`/repos/${activeRepoId}/gap-analysis/run`, { method: "POST" });
    await loadGapsPanel();
    toast(`Found ${result.applied} finding${result.applied === 1 ? "" : "s"}`, "ok");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function renderGapDocsIncluded() {
  const el = $("#gapDocsIncluded");
  if (!activeDocuments.length) {
    el.textContent = "Add at least one document (in the Docs tab) before running a comparison.";
    return;
  }
  const names = activeDocuments.map((d) => d.name).join(", ");
  el.textContent = `Comparing all ${activeDocuments.length} document${activeDocuments.length === 1 ? "" : "s"} together, as one corpus: ${names}`;
}

function renderGapSummary() {
  const total = activeGapFindings.length;
  const byCat = {};
  for (const f of activeGapFindings) byCat[f.category] = (byCat[f.category] || 0) + 1;
  $("#gapSummaryCards").innerHTML = [
    statCard(total, "Total gaps found"),
    statCard(byCat.missing_implementation || 0, "Missing implementation"),
    statCard(byCat.undocumented_capability || 0, "Undocumented capability"),
    statCard(byCat.mismatch || 0, "Mismatch"),
  ].join("");

  const breakdown = $("#gapBreakdown");
  if (!total) { breakdown.innerHTML = ""; return; }
  const max = Math.max(...Object.values(byCat));
  const rows = Object.entries(byCat)
    .sort((a, b) => b[1] - a[1])
    .map(
      ([cat, count]) => `
      <div class="rank-row">
        <div class="rank-body">
          <div class="rank-label"><span class="finding-cat cat-${cat}">${cat.replaceAll("_", " ")}</span></div>
          <div class="rank-bar-track"><div class="rank-bar" style="width:${Math.max(4, (count / max) * 100)}%"></div></div>
        </div>
        <span class="rank-score">${count}</span>
      </div>`
    )
    .join("");
  breakdown.innerHTML = `<div class="result-block"><h3>By category</h3><div class="rank-list">${rows}</div></div>`;
}

function renderGapFindingsList() {
  const catFilter = $("#gapFilterCategorySelect").value;
  const rows = activeGapFindings.filter((f) => !catFilter || f.category === catFilter);
  const el = $("#gapFindingsListAll");
  if (!rows.length) {
    el.innerHTML = `<p class="muted small">${activeGapFindings.length ? "No findings match this filter." : "No findings yet — run a comparison above."}</p>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (f) => `
      <div class="doc-gap-finding">
        <span class="finding-cat cat-${f.category}">${f.category.replaceAll("_", " ")}</span>
        <p>${escapeHtml(f.description)}</p>
      </div>`
    )
    .join("");
}

async function getGapContextForSelected() {
  if (!activeDocuments.length) { toast("Add at least one document first", "error"); return; }
  const el = $("#gapContextOutput");
  el.classList.remove("is-hidden");
  el.textContent = "Loading…";
  try {
    const ctx = await api(`/repos/${activeRepoId}/gap-analysis-context`);
    el.textContent = JSON.stringify(ctx, null, 2);
  } catch (e) {
    el.textContent = "Error: " + e.message;
  }
}

async function submitGapFindingsForSelected() {
  if (!activeDocuments.length) { toast("Add at least one document first", "error"); return; }
  const raw = $("#gapFindingsInput").value.trim();
  if (!raw) { toast("Paste the findings JSON first", "error"); return; }
  let findings;
  try {
    findings = JSON.parse(raw);
  } catch (e) {
    toast("That's not valid JSON: " + e.message, "error");
    return;
  }
  try {
    await api(`/repos/${activeRepoId}/gap-findings`, { method: "POST", body: JSON.stringify({ findings }) });
    $("#gapFindingsInput").value = "";
    await loadGapsPanel();
    toast("Findings saved", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
}

// --------------------------------------------------------------------------- LLM test case generation

let activeTestCases = [];
let testGenAvailable = null; // null = not checked yet

async function loadTestCasesPanel() {
  const repoId = activeRepoId;
  if (!activeDocuments.length) activeDocuments = await api(`/repos/${repoId}/documents`).catch(() => []);
  if (repoId !== activeRepoId) return; // stale response from a repo we've since navigated away from
  renderTestCaseDocsIncluded();

  if (testGenAvailable === null) {
    testGenAvailable = await api("/test-generation/config")
      .then((c) => c.automatic_available)
      .catch(() => false);
    if (repoId !== activeRepoId) return;
    $("#runTestGenBtn").classList.toggle("is-hidden", !testGenAvailable);
    $("#testGenHint").classList.toggle("is-hidden", !testGenAvailable);
    $("#testGenUnavailableHint").classList.toggle("is-hidden", testGenAvailable);
  }

  const cases = await api(`/repos/${repoId}/test-cases`).catch(() => []);
  if (repoId !== activeRepoId) return; // don't clobber a repo we've since navigated to with this one's results
  activeTestCases = cases;
  renderTestCaseSummary();
  renderTestCaseList();
}

async function runTestGeneration() {
  const btn = $("#runTestGenBtn");
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Generating…";
  try {
    const result = await api(`/repos/${activeRepoId}/test-cases/run`, { method: "POST" });
    await loadTestCasesPanel();
    toast(`Generated ${result.applied} test case${result.applied === 1 ? "" : "s"}`, "ok");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function renderTestCaseDocsIncluded() {
  const el = $("#testCaseDocsIncluded");
  if (!activeDocuments.length) {
    el.textContent = "Based on the codebase structure alone — no documents added yet. Add some (in the Docs tab) for richer, better-prioritized coverage.";
    return;
  }
  const names = activeDocuments.map((d) => d.name).join(", ");
  el.textContent = `Based on all ${activeDocuments.length} document${activeDocuments.length === 1 ? "" : "s"} and the full codebase structure: ${names}`;
}

function renderTestCaseSummary() {
  const total = activeTestCases.length;
  const byCat = {};
  for (const c of activeTestCases) byCat[c.category] = (byCat[c.category] || 0) + 1;
  $("#testCaseSummaryCards").innerHTML = [
    statCard(total, "Total test cases"),
    statCard(byCat.happy_path || 0, "Happy path"),
    statCard(byCat.edge_case || 0, "Edge case"),
    statCard(byCat.error_handling || 0, "Error handling"),
  ].join("");
}

function renderTestCaseList() {
  const catFilter = $("#testCaseFilterCategorySelect").value;
  const rows = activeTestCases.filter((c) => !catFilter || c.category === catFilter);
  const el = $("#testCaseListAll");
  if (!rows.length) {
    el.innerHTML = `<p class="muted small">${activeTestCases.length ? "No test cases match this filter." : "No test cases yet — generate some above."}</p>`;
    return;
  }
  el.innerHTML = rows.map(renderTestCaseCard).join("");
}

const TEST_CASE_ICONS = {
  happy_path: `<svg class="tc-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M8 12.5l2.5 2.5L16 9.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  edge_case: `<svg class="tc-icon" viewBox="0 0 24 24" fill="none"><path d="M12 3.5l9.5 16.5H2.5L12 3.5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v4M12 17h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  error_handling: `<svg class="tc-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M9 9l6 6M15 9l-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
};

function renderTestCaseCard(c) {
  return `
    <div class="test-case-card tc-${c.category}">
      <div class="test-case-head">
        ${TEST_CASE_ICONS[c.category] || ""}
        <h4>${escapeHtml(c.title)}</h4>
        <span class="finding-cat cat-${c.category}">${c.category.replaceAll("_", " ")}</span>
      </div>
      ${c.target ? `<div class="test-case-target">${escapeHtml(c.target)}</div>` : ""}
      ${c.preconditions ? `
        <div class="tc-section">
          <span class="tc-section-label">Preconditions</span>
          <p>${escapeHtml(c.preconditions)}</p>
        </div>` : ""}
      <div class="tc-section">
        <span class="tc-section-label">Steps</span>
        <ol class="tc-steps">${c.steps.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>
      </div>
      <div class="tc-section">
        <span class="tc-section-label">Expected result</span>
        <p class="tc-expected">${escapeHtml(c.expected_result)}</p>
      </div>
      ${c.edge_case_description ? `
        <div class="tc-section">
          <span class="tc-section-label">Edge case</span>
          <p class="tc-edge-case">${escapeHtml(c.edge_case_description)}</p>
        </div>` : ""}
    </div>`;
}

function renderDocsList() {
  const el = $("#docsList");
  if (!activeDocuments.length) {
    el.innerHTML = `<p class="muted small">No documents yet — add a high-level process/architecture doc describing the system overall.</p>`;
    return;
  }
  el.innerHTML = activeDocuments.map(renderDocCard).join("");
}

function renderDocCard(doc) {
  const preview = doc.content.length > 600 ? doc.content.slice(0, 600) + "…" : doc.content;
  return `
    <div class="doc-card" data-doc-id="${doc.id}">
      <div class="doc-card-head">
        <h4>${escapeHtml(doc.name)}</h4>
        <div class="doc-card-actions">
          <button type="button" class="btn" data-action="edit-doc">Edit</button>
          <button type="button" class="btn btn-danger" data-action="delete-doc">Delete</button>
        </div>
      </div>
      <div class="doc-card-content">${escapeHtml(preview)}</div>
    </div>`;
}

const _TEXT_FILE_RE = /\.(md|markdown|txt)$/i;
let pendingUploadFile = null; // a single non-text file (docx/pptx/xlsx/pdf) awaiting server-side extraction on submit
let pendingBulkFiles = [];    // 2+ files selected at once -- each becomes its own document, named from its filename

function openDocModal(doc = null) {
  $("#docForm").reset();
  editingDocId = doc ? doc.id : null;
  pendingUploadFile = null;
  pendingBulkFiles = [];
  $("#docModalTitle").textContent = doc ? "Edit document" : "Add document(s)";
  $("#docSubmitBtn").textContent = doc ? "Save changes" : "Save document";
  $("#docNameLabel").classList.remove("is-hidden");
  $("#docNameInput").required = true;
  $("#docContentLabel").hidden = false;
  $("#docFileSelectedNote").classList.add("is-hidden");
  $("#docBulkFileList").classList.add("is-hidden");
  $("#docBulkFileList").innerHTML = "";

  if (doc) {
    $("#docNameInput").value = doc.name;
    $("#docContentInput").value = doc.content;
  }
  $("#docModalBackdrop").hidden = false;
}
function closeDocModal() {
  $("#docModalBackdrop").hidden = true;
}

function handleDocFileInput(ev) {
  const files = Array.from(ev.target.files || []);
  if (!files.length) return;

  if (files.length > 1) {
    if (editingDocId) {
      // Editing replaces one document's content -- multi-select doesn't apply here.
      toast("Editing replaces one document -- pick a single file", "error");
      ev.target.value = "";
      return;
    }
    pendingUploadFile = null;
    pendingBulkFiles = files;
    $("#docNameLabel").classList.add("is-hidden");
    $("#docNameInput").required = false; // hiding the label alone doesn't exempt it from native validation
    $("#docContentLabel").hidden = true;
    $("#docFileSelectedNote").classList.add("is-hidden");
    const list = $("#docBulkFileList");
    list.innerHTML = files.map((f) => `<li>${escapeHtml(f.name)} <span class="muted small">(${(f.size / 1024).toFixed(1)} KB)</span></li>`).join("");
    list.classList.remove("is-hidden");
    $("#docSubmitBtn").textContent = `Add ${files.length} documents`;
    return;
  }

  // Exactly one file: keep the existing single-document flow (lets you
  // rename it or tweak content before saving).
  pendingBulkFiles = [];
  $("#docNameLabel").classList.remove("is-hidden");
  $("#docNameInput").required = true;
  $("#docBulkFileList").classList.add("is-hidden");
  $("#docBulkFileList").innerHTML = "";
  $("#docSubmitBtn").textContent = editingDocId ? "Save changes" : "Save document";

  const file = files[0];
  if (_TEXT_FILE_RE.test(file.name)) {
    // Plain text/Markdown: read client-side, no server round-trip needed.
    pendingUploadFile = null;
    $("#docContentLabel").hidden = false;
    $("#docFileSelectedNote").classList.add("is-hidden");
    const reader = new FileReader();
    reader.onload = () => {
      $("#docContentInput").value = reader.result;
      if (!$("#docNameInput").value) $("#docNameInput").value = file.name.replace(_TEXT_FILE_RE, "");
    };
    reader.onerror = () => toast("Couldn't read that file", "error");
    reader.readAsText(file);
  } else {
    // .docx/.pptx/.xlsx/.pdf: can't be read as text in the browser -- the
    // server extracts it on save (server/doc_extract.py).
    pendingUploadFile = file;
    $("#docContentInput").value = "";
    $("#docContentLabel").hidden = true;
    const note = $("#docFileSelectedNote");
    note.textContent = `Text will be extracted from "${file.name}" when you save.`;
    note.classList.remove("is-hidden");
    if (!$("#docNameInput").value) $("#docNameInput").value = file.name.replace(/\.[^.]+$/, "");
  }
}

async function submitDocForm(ev) {
  ev.preventDefault();

  if (pendingBulkFiles.length > 1) {
    const btn = $("#docSubmitBtn");
    btn.disabled = true;
    let ok = 0;
    const failed = [];
    for (const file of pendingBulkFiles) {
      btn.textContent = `Adding ${ok + failed.length + 1} of ${pendingBulkFiles.length}…`;
      try {
        const fd = new FormData();
        fd.append("file", file);
        await api(`/repos/${activeRepoId}/documents/upload`, { method: "POST", body: fd, headers: {} });
        ok++;
      } catch (e) {
        failed.push(`${file.name}: ${e.message}`);
      }
    }
    btn.disabled = false;
    closeDocModal();
    await loadDocsPanel();
    await loadGapsPanel();
    await loadTestCasesPanel();
    if (failed.length) toast(`Added ${ok} of ${pendingBulkFiles.length} — failed: ${failed.join("; ")}`, "error");
    else toast(`Added ${ok} document${ok === 1 ? "" : "s"}`, "ok");
    return;
  }

  const name = $("#docNameInput").value.trim();
  try {
    if (pendingUploadFile) {
      const fd = new FormData();
      fd.append("file", pendingUploadFile);
      if (name) fd.append("name", name);
      if (editingDocId) await api(`/documents/${editingDocId}/upload`, { method: "PUT", body: fd, headers: {} });
      else await api(`/repos/${activeRepoId}/documents/upload`, { method: "POST", body: fd, headers: {} });
    } else {
      const content = $("#docContentInput").value.trim();
      if (!content) { toast("Paste some content or choose a file first", "error"); return; }
      const payload = { name, content };
      if (editingDocId) await api(`/documents/${editingDocId}`, { method: "PUT", body: JSON.stringify(payload) });
      else await api(`/repos/${activeRepoId}/documents`, { method: "POST", body: JSON.stringify(payload) });
    }
    closeDocModal();
    await loadDocsPanel();
    await loadGapsPanel();
    toast(editingDocId ? "Document saved" : "Document added", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function deleteDoc(docId) {
  if (!confirm("Delete this document and its gap findings?")) return;
  await api(`/documents/${docId}`, { method: "DELETE" });
  await loadDocsPanel();
  await loadGapsPanel();
}

function wireDocsList() {
  $("#docsList").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-action]");
    if (!btn) return;
    const card = ev.target.closest(".doc-card");
    const docId = card.dataset.docId;
    const action = btn.dataset.action;
    if (action === "edit-doc") openDocModal(activeDocuments.find((d) => d.id === docId));
    else if (action === "delete-doc") deleteDoc(docId);
  });
}

// --------------------------------------------------------------------------- wire up

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  applyTheme(document.documentElement.dataset.theme || "dark");
  loadRepos();

  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#backToDashboard").addEventListener("click", showDashboard);
  $("#addRepoBtn").addEventListener("click", () => openRepoModal());
  $("#cancelRepoBtn").addEventListener("click", closeRepoModal);
  $("#repoModalBackdrop").addEventListener("click", (e) => { if (e.target.id === "repoModalBackdrop") closeRepoModal(); });
  $("#repoForm").addEventListener("submit", submitRepoForm);
  $all("#sourceSegmented .seg-btn").forEach((b) => b.addEventListener("click", () => setSourceType(b.dataset.value)));
  $("#testAdoBtn").addEventListener("click", testAdoFromModal);
  $("#testGithubBtn").addEventListener("click", testGithubFromModal);

  $("#runBtn").addEventListener("click", runAnalysis);
  $("#testConnBtn").addEventListener("click", testConnection);
  $("#deleteRepoBtn").addEventListener("click", deleteRepo);
  $("#editRepoBtn").addEventListener("click", () => openRepoModal(repos.find((r) => r.id === activeRepoId)));

  $("#findingsRunSelect").addEventListener("change", loadFindingsPanel);
  $("#findingsCategoryFilter").addEventListener("change", () => loadFindingsPanel());
  $("#graphRunSelect").addEventListener("change", loadGraphPanel);
  $all("#graphSourceSegmented .seg-btn").forEach((b) => b.addEventListener("click", () => setGraphSource(b.dataset.value)));
  $("#insightsRunSelect").addEventListener("change", loadInsightsPanel);
  $("#compareRunBtn").addEventListener("click", runCompare);

  $("#addDocBtn").addEventListener("click", () => openDocModal());
  $("#cancelDocBtn").addEventListener("click", closeDocModal);
  $("#docModalBackdrop").addEventListener("click", (e) => { if (e.target.id === "docModalBackdrop") closeDocModal(); });
  $("#docForm").addEventListener("submit", submitDocForm);
  $("#docFileInput").addEventListener("change", handleDocFileInput);
  wireDocsList();

  $("#runAutoGapBtn").addEventListener("click", runAutoGapAnalysis);
  $("#getGapContextBtn").addEventListener("click", getGapContextForSelected);
  $("#submitGapFindingsBtn").addEventListener("click", submitGapFindingsForSelected);
  $("#gapFilterCategorySelect").addEventListener("change", renderGapFindingsList);

  $("#runTestGenBtn").addEventListener("click", runTestGeneration);
  $("#testCaseFilterCategorySelect").addEventListener("change", renderTestCaseList);
});
