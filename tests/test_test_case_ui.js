// Pure presentation rules shared by the suite and export UI.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const start = source.indexOf('function testCaseAssessment(c)');
const end = source.indexOf('function updateExportLinks(', start);
const context = {};
vm.runInNewContext(source.slice(start, end), context);
const imported = { title: 'Reject negative amount', created_via: 'imported', source_doc_id: 'story', status: 'contradicted', covers: ['Charge'], module: 'billing' };
assert(context.matchesTestCase(imported, {source:'imported'}));
assert(!context.matchesTestCase(imported, {source:'generated'}));
assert(!context.matchesTestCase(imported, {source:'story'}));
assert(context.matchesTestCase(imported, {source:'imported',q:'negative',assessment:'conflict',module:'billing',story:'story'}));
assert.equal(context.testCaseAssessmentLabel({...imported,is_stale:true}), 'Needs reassessment');
assert.equal(context.testCaseAssessmentLabel({...imported,covers:[]}), 'Insufficient evidence');
assert.equal(context.testCaseAssessmentLabel({...imported,status:null}), 'Not assessed');
assert.equal(context.testCaseAssessmentLabel({...imported,status:'supported',assessment_kind:'inferred_from_requirement',judgment_source:'human_confirmation'}), 'Inferred support');
assert.equal(context.testCaseAssessmentLabel({...imported,status:'supported'}), 'AI-supported');
console.log('Test-case presentation rules: 9 assertions passed');

const disputed = {...imported,status:'unknown',review_state:{state:'disputed',label:'Disputed by reviewer'},human_review:{decision:'rejected_ai_judgment'}};
assert.equal(context.testCaseReviewLabel(disputed),'Disputed by reviewer');
assert(context.matchesTestCase(disputed,{assessment:'disputed'}));
assert(!context.matchesTestCase(imported,{assessment:'disputed'}));
assert.equal(context.testCaseAssessment(disputed),'inconclusive');
console.log('Human review presentation: 4 assertions passed');

for (const outcome of ['implementation_and_test_conflict','conflicts_found']) {
  const c = {...imported,reassessment:{outcome}};
  assert.equal(context.testCaseAssessment(c),'conflict');
  assert(!context.testCaseAssessmentLabel(c).includes('clarification'));
}
assert.equal(context.testCaseAssessment({...imported,reassessment:{outcome:'unvalidated_conflict_claims'}}),'inconclusive');
assert.equal(context.testCaseAssessmentLabel({...imported,reassessment:{outcome:'unvalidated_conflict_claims'}}),'Earlier conflict claims · reassess');
console.log('Three-way conflict presentation: 6 assertions passed');

assert.equal(context.testCaseAssessmentLabel({...imported,reassessment:{outcome:'conflicts_found',outcome_label:'Test conflicts with code · requirements unresolved'}}),'Test conflicts with code · requirements unresolved');
console.log('Combination-specific label: passed');
