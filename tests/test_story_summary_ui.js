// Exercise the real Story Coverage summary, including human changes and stale snapshots.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const panel={innerHTML:''};
const context={activeStoryRequirements:[],statCard:(value,label,detail)=>`${label}:${value}:${detail||''}\n`,$:()=>panel};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderStoryCoverageSummary()'),source.indexOf('function renderStoryRequirementsList()')),context);
const criterion={status:'unknown',ai_status:'unknown',ai_evidence_level:'inconclusive',is_stale:false,tested:false};
context.activeStoryRequirements=[criterion];context.renderStoryCoverageSummary();
const before=panel.innerHTML;
context.activeStoryRequirements=[{...criterion,status:'supported',source:'human_confirmed'}];context.renderStoryCoverageSummary();
assert.equal(panel.innerHTML,before);
assert(panel.innerHTML.includes('AI-supported:0%'));
context.activeStoryRequirements=[
 {...criterion,status:'supported',ai_evidence_level:'ai_supported',is_stale:true},
 {...criterion,status:'supported',ai_evidence_level:'ai_assessment_unavailable'},
 {...criterion,status:'supported',ai_evidence_level:'insufficient_evidence'},
 {...criterion,status:'contradicted',ai_evidence_level:'ai_conflict'},
 {...criterion,status:'supported',ai_evidence_level:'ai_supported',tested:true},
];
context.renderStoryCoverageSummary();
assert(panel.innerHTML.includes('AI-supported:20%'));
assert(panel.innerHTML.includes('Unknown / unavailable:2'));
assert(panel.innerHTML.includes('Needs reassessment:1'));
assert(panel.innerHTML.includes('Contradicted:1'));
assert(panel.innerHTML.includes('Case linkage:100%'));
assert(panel.innerHTML.includes('1 stale excluded from support'));
context.activeStoryRequirements=[];context.renderStoryCoverageSummary();
assert(!panel.innerHTML.includes('NaN'));
context.escapeHtml=String;
vm.runInContext(source.slice(source.indexOf('function renderSourceContext(context)'),source.indexOf('function renderCaseReassessment(')),context);
const draft=context.renderSourceContext({scope:'document_generation',document_id:'doc',document_fingerprint:'abc123'});
assert(draft.includes('Draft source: user-story document'));
assert(draft.includes('No code assessment or test execution is implied'));
assert(draft.includes('abc123'));
console.log('Story Coverage summary and document-draft provenance: 12 assertions passed');
