// Disabled provider actions stay visible; their metered API calls remain gated.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const ids=['generateTestCasesAllBtn','verifyTestCasesAllBtn','verifyImportedTestCasesBtn'];
const elements=Object.fromEntries(ids.map(id=>[id,{hidden:true,disabled:false,classList:{remove(name){elements[id].hidden=false;}}}]));
const context={readOnly:false,testGenAvailable:false,activeDocuments:[{id:'doc'}],activeTestCases:[{source_doc_id:'doc'},{created_via:'imported'}],$:s=>elements[s.slice(1)]};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function updateTestCaseToolAvailability()'),source.indexOf('// Shown on the Test Cases tab while modules')),context);
context.updateTestCaseToolAvailability();
for(const id of ids){assert(!elements[id].hidden);assert(elements[id].disabled);}
context.testGenAvailable=true;context.updateTestCaseToolAvailability();
for(const id of ids)assert(!elements[id].disabled);
context.activeTestCases=[];context.updateTestCaseToolAvailability();
assert(!elements.generateTestCasesAllBtn.disabled);assert(elements.verifyTestCasesAllBtn.disabled);assert(elements.verifyImportedTestCasesBtn.disabled);
context.readOnly=true;context.updateTestCaseToolAvailability();
for(const id of ids)assert(elements[id].disabled);
console.log('Import/generation/assessment action discoverability: 15 assertions passed');
