// Execute the real SVG renderers against empty, stale, populated and switched data.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const markup=fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
const nodes={};const $=selector=>nodes[selector]??=( {innerHTML:'',textContent:''} );
const context={activeDashboard:{},$,escapeHtml:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const TD_HEALTH_COLOR'),source.indexOf('function tdPct(')),context);
vm.runInContext(source.slice(source.indexOf('function tdCount('),source.indexOf('function renderDashboardTrend()')),context);
const req=(total,by_status={},stale=0,ai_unavailable=0)=>({total,by_status,evidence:{stale},ai_unavailable});
let checks=0;function ok(value){assert(value);checks++;}
// The exact reported failure: the heading has 9 criteria, all current statuses zero.
context.activeDashboard={requirements:req(9,{},9)};
context.renderDashboardRequirementStatusChart();
let html=$('#tdReqStatusChart').innerHTML;
ok(html.includes('data-status="stale" data-count="9"'));
ok(html.includes('All 9 criteria need reassessment'));
ok(html.includes('<svg'));ok(!html.includes('<canvas'));
ok($('#tdReqTotal').textContent==='9 total');
// Empty -> populated: the host is retained and can render the next repository.
context.activeDashboard={requirements:req(0)};context.renderDashboardRequirementStatusChart();
ok($('#tdReqStatusChart').innerHTML.includes('No acceptance criteria'));
context.activeDashboard={requirements:req(6,{supported:2,contradicted:1,unknown:1},1,1)};
context.renderDashboardRequirementStatusChart();html=$('#tdReqStatusChart').innerHTML;
ok(html.includes('data-status="supported" data-count="2"'));
ok(html.includes('data-status="unavailable" data-count="1"'));
ok(html.includes('data-status="stale" data-count="1"'));
ok([...html.matchAll(/data-count="(\d+)"/g)].reduce((sum,m)=>sum+Number(m[1]),0)===6);
ok(!html.includes('NaN'));
// Theme repaint and repeat loading need no Chart global, canvas or remote library.
context.renderDashboardRequirementStatusChart();ok($('#tdReqStatusChart').innerHTML===html);
ok(html.includes('var(--ok)'));ok(html.includes('Source-supported'));
// A legacy total without recorded classifications must not become apparent support.
context.activeDashboard={requirements:req(4)};context.renderDashboardRequirementStatusChart();
ok($('#tdReqStatusChart').innerHTML.includes('data-status="unavailable" data-count="4"'));
context.activeDashboard={requirements:req(2,{supported:3})};context.renderDashboardRequirementStatusChart();
ok($('#tdReqStatusChart').innerHTML.includes('could not be reconciled'));
// Strict numbers do not use human-confirmed live counts.
const input=req(3,{supported:1,unknown:2});input.human_confirmed=3;
context.activeDashboard={requirements:input};context.renderDashboardRequirementStatusChart();
ok($('#tdReqStatusChart').innerHTML.includes('data-status="supported" data-count="1"'));
// Fresh denominators and separate provenance remain visible in the linkage lanes.
const byOrigin={generated:{total:8,fresh:6,stale:2,fresh_linked:4,fresh_unlinked:2,pct:66.7},imported:{total:1,fresh:1,stale:0,fresh_linked:1,fresh_unlinked:0,pct:100}};
context.tdRenderOriginCoverageChart('lane','No cases',byOrigin);html=$('#lane').innerHTML;
ok(html.includes('5 linked and 2 unlinked among 7 fresh cases'));
ok(html.includes('4/6 linked'));ok(html.includes('1/1 linked'));ok(html.includes('2 stale excluded'));
context.tdRenderOriginCoverageChart('lane','No cases',{generated:{...byOrigin.generated,fresh:0,fresh_linked:0,stale:8},imported:{...byOrigin.imported,fresh:0,fresh_linked:0,stale:1}});
ok($('#lane').innerHTML.includes('9 stale cases excluded'));
context.tdRenderOriginCoverageChart('lane','No cases',byOrigin);ok($('#lane').innerHTML.includes('<svg'));
// Missing priorities are displayed, not silently lost from the total.
context.activeDashboard={test_cases:{total:4,by_priority:{1:2,2:1},by_category:{happy_path:3}}};
context.renderDashboardPriorityChart();ok($('#tdPriorityChart').innerHTML.includes('Unspecified'));
context.renderDashboardCategoryChart();ok($('#tdCategoryChart').innerHTML.includes('Unclassified'));
ok($('#tdPriorityChart').innerHTML.includes('td-lollipop-row'));
// SVG hosts replace all old canvas IDs and the blocking chart CDN is gone.
for(const id of ['tdReqStatusChart','tdHealthChart','tdCategoryChart','tdPriorityChart','tdCaseOriginChart','tdDocOriginChart','tdStoryLinkChart','tdCodeLinkChart'])ok(markup.includes(`id="${id}"`));
ok(!markup.includes('<canvas'));ok(!markup.includes('cdnjs.cloudflare.com/ajax/libs/Chart'));
console.log(`Dashboard evidence charts: ${checks} assertions passed`);
// Dashboard signals remain source-only when current human decisions differ.
vm.runInContext(source.slice(source.indexOf('function tdPct('),source.indexOf('async function loadDashboardPanel()')),context);
vm.runInContext(source.slice(source.indexOf('function renderDashboardBanner()'),source.indexOf('// Local SVG charts:')),context);
context.activeDashboard={overall:{status:'red',headline:'A potential implementation conflict needs review'},documents:{total:3,by_health:{verified:3},by_ai_health:{ai_assessment_unavailable:3},by_origin:{manual:3}},requirements:req(9,{},9),test_cases:{total:47,by_category:{happy_path:19,error_handling:16,edge_case:12},by_priority:{1:19,2:20,3:8},by_origin:{generated:46,imported:1},evidence:{stale:2}}};
context.renderDashboardStatCards();
assert($('#tdStatCards').innerHTML.includes('Acceptance criteria'));
assert($('#tdStatCards').innerHTML.includes('>47</strong>'));
assert(!$('#tdAssessmentSignals').innerHTML.includes('100%'));
assert($('#tdAssessmentSignals').innerHTML.includes('No current supported/contradicted judgments'));
assert(!$('#tdAssessmentSignals').innerHTML.includes('0/0'));
context.renderDashboardBanner();assert($('#tdBanner').innerHTML.includes('What this status means'));
// Print expands all details and restores each original state after printing.
const details=[{open:false},{open:true},{open:false}];let afterPrint,printCalls=0,printing=false;
nodes['#panel-traceability']={classList:{add(){printing=true;},remove(){printing=false;}}};
context.$all=()=>details;
context.requestAnimationFrame=callback=>callback();
context.window={addEventListener:(name,callback)=>{afterPrint=callback;},removeEventListener:()=>{},print:()=>{assert(details.every(d=>d.open));assert(printing);printCalls++;}};
vm.runInContext(source.slice(source.indexOf('function exportDashboardPdf()'),source.indexOf('async function loadTestCasesPanel()')),context);
context.exportDashboardPdf();assert.equal(printCalls,1);afterPrint();
assert.deepEqual(details.map(d=>d.open),[false,true,false]);assert(!printing);
console.log('Dashboard source-signal and PDF state: 11 assertions passed');
