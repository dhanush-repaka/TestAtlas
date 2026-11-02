// User-facing evidence semantics, independent of browser or network services.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const panel = {innerHTML: ''};
const context = {escapeHtml: String, readOnly: false, storyCoverageAvailable: false,
  renderSourceContext: () => '', $: () => panel, statCard: (v,l,d='') => `${l}:${v}:${d}\n`};
vm.createContext(context);
function load(start, end) { vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context); }
load('function renderStoryRequirementCard(', 'async function getStoryCoverageContextForSelected(');
let html = context.renderStoryRequirementCard({id:'r', doc_id:'d', status:'supported', is_stale:true, covers:[]});
assert(html.indexOf('Needs reassessment') < html.indexOf('Historical judgment: supported'));
assert(html.includes('disabled>Generate test case'));
html = context.renderStoryRequirementCard({id:'r', doc_id:'d', status:'supported', covers:[]});
assert(html.includes('Generation unavailable'));
assert(html.includes('disabled>Generate test case'));
context.storyCoverageAvailable = true;
html = context.renderStoryRequirementCard({id:'r', doc_id:'d', status:'supported', covers:[]});
assert(!html.includes('disabled>Generate test case'));
const gapSummary = {innerHTML:""}, gapBreakdown = {innerHTML:""};
context.$ = selector => selector === "#gapSummaryCards" ? gapSummary : gapBreakdown;
context.activeGapFindings = []; context.activeGapAssessment = null;
load('function renderGapSummary()', 'function renderGapFindingsList()');
context.renderGapSummary();
assert(gapSummary.innerHTML.includes('Saved gaps:—:Not assessed'));
context.activeGapAssessment = {assessed_at:'today'};
context.renderGapSummary();
assert(gapSummary.innerHTML.includes('Saved gaps:0:Historical comparison'));
load('function tdPriorityLabel(', 'function renderDashboardCategoryChart(');
assert.equal(context.tdPriorityLabel('3'), 'P3');
assert(!context.tdPriorityLabel('3').includes('common'));
console.log('Readiness evidence and unavailable-action semantics: 10 assertions passed');
