// Real renderer/request logic: canonical state and the DOM version must win over raw status/cache.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context={readOnly:false,testGenAvailable:true,TEST_CASE_ICONS:{happy_path:''},escapeHtml:esc,moduleDisplayName:s=>s,
 testCaseOriginClass:()=>'',testCaseOriginLabel:()=>'',testCaseAssessmentLabel:c=>c.assessment.label,testCaseReviewLabel:()=>''};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderTestCaseDetails(c)'),source.indexOf('function renderSourceContext(')),context);
const c={id:'case',status:'unknown',category:'happy_path',steps:[],covers:[],
 assessment:{state:'conflict',label:'Canonical conflict',version:'displayed-version',reviewable:true,agreement_decision:'confirmed_contradicted'}};
const html=context.renderTestCaseDetails(c);
assert(html.includes('data-decision="confirmed_contradicted"'));
assert(html.includes('data-assessment-version="displayed-version"'));
assert(!/data-decision="confirmed_contradicted"[^>]*disabled/.test(html));
assert(/data-decision="confirmed_contradicted"[^>]*disabled/.test(context.renderTestCaseDetails({...c,is_stale:true})));
let submitted;
const button={dataset:{decision:'confirmed_contradicted'}};
const group={dataset:{assessmentVersion:'displayed-version'},querySelectorAll:()=>[button]};
const note={value:'I reviewed this conflict',focus:()=>{}};
button.closest=sel=>sel==='.tc-review-btns'?group:{querySelector:()=>note};
Object.assign(context,{activeRepoId:'repo',activeTestCases:[{...c,assessment:{...c.assessment,version:'newer-cached-version'}}],
 renderTestCaseList:()=>{},toast:()=>{},api:async(url,options)=>{submitted=JSON.parse(options.body);return {decision:submitted.decision,assessment_version:submitted.assessment_version,review_state:{state:'confirmed'}};}});
vm.runInContext(source.slice(source.indexOf('const caseReviewsInFlight ='),source.indexOf('// Four mutually-exclusive origins')),context);
(async()=>{
 await context.recordTestCaseReview('case','confirmed_contradicted',button);
 assert.equal(submitted.assessment_version,'displayed-version');
 assert.equal(submitted.note,'I reviewed this conflict');
 assert.equal(submitted.decision,'confirmed_contradicted');
 assert.equal(context.activeTestCases[0].review_state.state,'previous_assessment');
 console.log('Review renderer and version-bound request: 8 assertions passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
