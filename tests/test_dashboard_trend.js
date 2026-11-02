// Dependency-free renderer regression: node tests/test_dashboard_trend.js
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const start = source.indexOf('function renderDashboardTrend()');
const end = source.indexOf('\nfunction renderDashboardGapList', start);
function render(trend) {
  const el = { innerHTML: '' };
  const context = { activeDashboard: { trend }, $: () => el, tdCssVar: () => '#123456', escapeHtml: (s) => s };
  vm.runInNewContext(source.slice(start, end) + '\nrenderDashboardTrend();', context);
  return el.innerHTML;
}
const legacy = { date: '2026-10-30', summary: { documents: { verified_pct: 99, coded_pct: 40 }, requirements: { correctness_rate: 98 } } };
const current = { date: '2026-11-01', summary: { evidence_schema_version: 2, documents: { ai_verified_pct: 50, coded_pct: 70 }, requirements: { correctness_rate: 60 } } };
let html = render([legacy, current]);
assert(!html.includes('99%'));
assert(!html.includes('98%'));
assert(!html.includes('NaN'));
assert(html.includes('50%'));
assert(html.includes('60%'));
html = render([legacy, { ...legacy, date: '2026-10-31' }]);
assert(html.includes('Unavailable'));
assert(!html.includes('NaN'));
// A missing historical value must split, rather than bridge, the AI line.
html = render([current, legacy, current]);
const points = [...html.matchAll(/<polyline points="([^"]*)"/g)].map((m) => m[1]);
assert.equal(points.filter((p) => p.split(' ').length === 3).length, 1); // code-evidence series only
assert.equal(points.filter((p) => p.split(' ').length === 1).length, 4); // two separate AI series
console.log('Dashboard trend renderer: 3 scenarios passed');

const unavailable = {date:'2026-11-02', summary:{evidence_schema_version:2,documents:{total:3,by_ai_health:{verified:0,needs_reassessment:3},ai_verified_pct:0,coded_pct:100},requirements:{correctness_rate:0,correctness_rate_denominator:0}}};
html = render([current, unavailable]);
assert.equal((html.match(/Unavailable/g)||[]).length,2);
assert(html.includes('Supported criteria / assessed criteria'));
assert(html.includes('Stories with saved code links / all stories'));
console.log('Trend unavailable-denominator semantics: 3 assertions passed');
