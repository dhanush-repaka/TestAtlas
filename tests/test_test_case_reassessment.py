"""Three-way decisions, evidence boundaries, preservation and reassessment jobs."""
import copy
import json
import os
import unittest
from unittest import mock

from server import db, app as server_app, test_case_reassessment as reassess, test_case_view
from server.llm_test_verification import VerificationResult
from tests import test_document_test_verification as api_fixture

CONTEXT = {'documents': [{'id':'story', 'name':'Checkout story', 'content':'Reject orders above 10 items.'}],
           'modules':[{'module':'cart', 'files':[{'file':'cart.py','functions':['checkout'],'classes':[]}]}]}
SOURCES = {'checkout':'def checkout(items):\n    return len(items) <= 10\n'}
CASE = {'id':'case', 'title':'Reject large order', 'steps':[{'action':'Order 11','expected':'Rejected'}], 'expected_result':'Rejected'}


def findings(r='supported', c='contradicted', i='contradicted'):
    story = {'kind':'story','id':'story','quote':'Reject orders above 10 items.'}
    code = {'kind':'code','id':'checkout','quote':'return len(items) <= 10'}
    result = {'requirement_alignment':{'status':r,'rationale':'Expectation vs current story','citations':[story]},
            'code_evidence':{'status':c,'rationale':'Actual behaviour vs expectation','citations':[code]},
            'implementation_alignment':{'status':i,'rationale':'Implementation vs intended behaviour','citations':[story,code]}}
    for key, entry in result.items():
        if entry['status']=='contradicted':
            entry['conflict']={'left_quote':story['quote'] if key=='implementation_alignment' else CASE['expected_result'],
                               'right_quote':story['quote'] if key=='requirement_alignment' else code['quote'],
                               'difference':'The synthetic comparison identifies opposing expected outcomes.'}
    return result


class DecisionTests(unittest.TestCase):
    def check(self, raw, case=None, context=None, sources=None):
        return reassess.validate_report(raw, case or CASE, context or CONTEXT, SOURCES if sources is None else sources)

    def test_regression_preserves_expectation(self):
        before = copy.deepcopy(CASE)
        report = self.check(findings())
        self.assertEqual(report['outcome'], 'potential_regression')
        self.assertEqual(CASE, before)

    def test_current_story_and_code_agree_but_case_does_not(self):
        self.assertEqual(self.check(findings('contradicted','contradicted','supported'))['outcome'], 'test_case_needs_update')

    def test_changed_story_and_previously_aligned_case(self):
        old = self.check(findings('supported','supported','supported'))
        old['story_versions']['story'] = 'previous-story-version'
        report = self.check(findings('contradicted','contradicted','supported'), {**CASE,'reassessment':old})
        self.assertEqual(report['outcome'], 'outdated_test_case')

    def test_no_prior_story_baseline_does_not_claim_requirement_changed(self):
        self.assertEqual(self.check(findings('contradicted','contradicted','supported'))['outcome'], 'test_case_needs_update')

    def test_missing_story_is_not_a_pass(self):
        report = self.check(findings('supported','supported','supported'), context={**CONTEXT,'documents':[]})
        self.assertEqual(report['requirement_alignment']['status'], 'unknown')
        self.assertEqual(report['outcome'], 'needs_clarification')

    def test_names_without_real_source_are_insufficient(self):
        report = self.check(findings(), sources={})
        self.assertEqual(report['code_evidence']['status'], 'unknown')
        self.assertEqual(report['outcome'], 'needs_clarification')

    def test_invented_quotes_are_demoted(self):
        raw = findings()
        raw['requirement_alignment']['citations'][0]['quote'] = 'Allow 999 items'
        self.assertEqual(self.check(raw)['requirement_alignment']['status'], 'unknown')

    def test_explicit_source_document_cannot_be_substituted(self):
        report = self.check(findings(), {**CASE,'source_doc_id':'different-story'})
        self.assertEqual(report['requirement_alignment']['status'], 'unknown')

    def test_mixed_findings_preserve_the_established_conflict(self):
        self.assertEqual(self.check(findings('supported','contradicted','supported'))['outcome'], 'conflicts_found')

    def test_code_derived_bug_case_flags_both_requirement_conflicts(self):
        report=self.check(findings('contradicted','supported','contradicted'))
        self.assertEqual(report['outcome'],'implementation_and_test_conflict')
        self.assertEqual(report['conflict_dimensions'],['requirement_alignment','implementation_alignment'])
        self.assertEqual(report['clarification_reasons'],[])

    def test_all_contradicted_does_not_request_business_clarification(self):
        report=self.check(findings('contradicted','contradicted','contradicted'))
        self.assertEqual(report['outcome'],'conflicts_found')
        self.assertEqual(report['unresolved_dimensions'],[])
        self.assertEqual(report['clarification_reasons'],[])

    def test_all_27_status_combinations_keep_any_validated_conflict_visible(self):
        import itertools
        for statuses in itertools.product(('supported','contradicted','unknown'),repeat=3):
            with self.subTest(statuses=statuses):
                report=self.check(findings(*statuses))
                if 'contradicted' in statuses:
                    self.assertNotIn(report['outcome'],('needs_clarification','aligned','unvalidated_conflict_claims'))
                    self.assertEqual(len(report['conflict_dimensions']),statuses.count('contradicted'))
                elif statuses==('supported',)*3:
                    self.assertEqual(report['outcome'],'aligned')
                else:
                    self.assertEqual(report['outcome'],'needs_clarification')
                    self.assertTrue(report['clarification_reasons'])

    def test_conflict_survives_missing_other_comparison_and_different_criterion(self):
        raw=findings('contradicted','unknown','supported')
        raw['implementation_alignment']['citations']=[]
        report=self.check(raw)
        self.assertEqual(report['outcome'],'conflicts_found')
        self.assertEqual(report['conflict_dimensions'],['requirement_alignment'])
        self.assertTrue(report['clarification_reasons'])

    def test_saved_results_get_new_display_without_rewriting_history(self):
        report=self.check(findings('contradicted','supported','contradicted'))
        report['outcome']='needs_clarification'
        case={**CASE,'reassessment':report}
        before=copy.deepcopy(case)
        displayed=test_case_view.enrich_cases([case],None)[0]
        self.assertEqual(displayed['reassessment']['outcome'],'implementation_and_test_conflict')
        self.assertEqual(displayed['reassessment']['recorded_outcome'],'needs_clarification')
        self.assertEqual(case,before)

    def test_old_unexplained_conflicts_are_not_promoted_to_validated_conflicts(self):
        report=self.check(findings('contradicted','contradicted','contradicted'))
        for key in reassess.DIMENSIONS:
            report[key].pop('conflict',None)
        displayed=reassess.display_report(report)
        self.assertEqual(displayed['outcome'],'unvalidated_conflict_claims')
        self.assertEqual(displayed['conflict_dimensions'],[])
        self.assertEqual(test_case_view.assessment_state({'reassessment':displayed}),'inconclusive')

    def test_explicit_outcome_for_every_combination(self):
        expected = {
            'SSS':'aligned', 'SSC':'conflicts_found', 'SSU':'needs_clarification',
            'SCS':'conflicts_found', 'SCC':'potential_regression', 'SCU':'conflicts_found',
            'SUS':'needs_clarification', 'SUC':'conflicts_found', 'SUU':'needs_clarification',
            'CSS':'conflicts_found', 'CSC':'implementation_and_test_conflict', 'CSU':'conflicts_found',
            'CCS':'test_case_needs_update', 'CCC':'conflicts_found', 'CCU':'conflicts_found',
            'CUS':'conflicts_found', 'CUC':'conflicts_found', 'CUU':'conflicts_found',
            'USS':'needs_clarification', 'USC':'conflicts_found', 'USU':'needs_clarification',
            'UCS':'conflicts_found', 'UCC':'conflicts_found', 'UCU':'conflicts_found',
            'UUS':'needs_clarification', 'UUC':'conflicts_found', 'UUU':'needs_clarification',
        }
        self.assertEqual(set(expected),set(reassess.COMBINATION_RULES))
        statuses={'S':'supported','C':'contradicted','U':'unknown'}
        labels=set()
        for combination,outcome in expected.items():
            with self.subTest(combination=combination):
                report=self.check(findings(*(statuses[c] for c in combination)))
                self.assertEqual(report['status_combination'],combination)
                self.assertEqual(report['outcome'],outcome)
                self.assertTrue(report['recommended_action'])
                self.assertTrue(report['outcome_label'])
                labels.add(report['outcome_label'])
                saved=reassess.display_report(report)
                self.assertEqual(saved['outcome_label'],report['outcome_label'])
                self.assertEqual(saved['recommended_action'],report['recommended_action'])
        self.assertEqual(len(labels),27)

    def test_combined_diagnosis_requires_shared_acceptance_criterion(self):
        report=self.check(findings('contradicted','supported','contradicted'))
        report['implementation_alignment']['citations']=[c for c in report['implementation_alignment']['citations'] if c['kind']=='code']
        displayed=reassess.display_report(report)
        self.assertEqual(displayed['outcome'],'conflicts_found')
        self.assertIn('different comparison scope',displayed['outcome_label'])
        self.assertEqual(len(displayed['conflict_dimensions']),2)

    def test_every_origin_gets_all_three_checks(self):
        for extra in ({'created_via':'imported'}, {'created_via':'generated'}, {'source_doc_id':'story'}, {'source_journey_id':'journey'}):
            self.assertEqual(self.check(findings(), {**CASE,**extra})['outcome'], 'potential_regression')

    def test_document_and_case_changes_make_assessment_stale(self):
        case = {**CASE,'run_id':'run','reassessment':self.check(findings())}
        self.assertFalse(test_case_view.enrich_cases([case],'run',CONTEXT['documents'])[0]['is_stale'])
        docs = [{**CONTEXT['documents'][0],'content':'Allow up to 20 items'}]
        self.assertTrue(test_case_view.enrich_cases([case],'run',docs)[0]['is_stale'])
        self.assertTrue(test_case_view.enrich_cases([{**case,'expected_result':'Allowed'}],'run',CONTEXT['documents'])[0]['is_stale'])

    def test_malformed_citation_cannot_crash_or_pass(self):
        raw = findings()
        raw['requirement_alignment']['citations'] = [{'kind':'story','id':[],'quote':'Reject'}]
        self.assertEqual(self.check(raw)['requirement_alignment']['status'], 'unknown')

    def test_ambiguous_code_name_cannot_back_a_finding(self):
        context = copy.deepcopy(CONTEXT)
        context['modules'].append({'module':'other','files':[{'file':'other.py','functions':['checkout'],'classes':[]}]})
        self.assertEqual(self.check(findings(),context=context)['code_evidence']['status'],'unknown')

    def test_different_criteria_cannot_be_combined_into_a_pass(self):
        raw = findings('supported','supported','supported')
        context = copy.deepcopy(CONTEXT)
        context['documents'][0]['content'] += ' Payment requires a valid card.'
        raw['implementation_alignment']['citations'] = copy.deepcopy(raw['implementation_alignment']['citations'])
        raw['implementation_alignment']['citations'][0]['quote'] = 'Payment requires a valid card.'
        self.assertEqual(self.check(raw,context=context)['outcome'],'needs_clarification')



class ReassessmentApiTests(unittest.TestCase):
    setUp = api_fixture.VerifyCasesForDocumentApiTests.setUp
    tearDown = api_fixture.VerifyCasesForDocumentApiTests.tearDown

    def create_case(self, **source):
        run = db.get_latest_successful_run(self.repo_id)
        return db.create_test_case(self.repo_id,'cart',run['id'],{**CASE,'category':'happy_path','covers':['Cart.add']}, **source)

    def test_reassessment_appends_history_without_editing_or_deleting_reviews(self):
        c = self.create_case()
        run = db.get_latest_successful_run(self.repo_id)
        before = db.list_test_cases(self.repo_id)[0]
        review = self.client.post(f"/api/repos/{self.repo_id}/test-cases/{c['id']}/review",json={'decision':'needs_more_info','note':'Keep this review'}).json()
        report = reassess.validate_report(findings(), before, CONTEXT, SOURCES)
        for _ in range(2):
            db.update_test_case_verification(c['id'],'contradicted','Changed code',['Cart.add'],'cart',
                assessed_model='test-model',run_id=run['id'],reassessment=report,
                expected_case_fingerprint=reassess.case_fingerprint(before))
        after = db.list_test_cases(self.repo_id)[0]
        for key in ('id','title','steps','expected_result','description','preconditions','created_at','created_via'):
            self.assertEqual(after.get(key),before.get(key))
        history = self.client.get(f"/api/repos/{self.repo_id}/test-cases/{c['id']}/assessments").json()
        self.assertEqual(len(history),2)
        self.assertEqual(db.list_human_reviews(self.repo_id)[0]['id'],review['id'])
        self.assertEqual(history[-1]['snapshot']['previous_case']['status'],None)
        self.assertEqual(history[0]['snapshot']['previous_case']['status'],'contradicted')

    def test_mid_assessment_case_edit_is_not_overwritten(self):
        c = self.create_case()
        original = db.list_test_cases(self.repo_id)[0]
        with db.get_conn() as conn:
            conn.execute('UPDATE test_cases SET expected_result = ? WHERE id = ?',('Human edit',c['id']))
        with self.assertRaisesRegex(RuntimeError,'changed during'):
            db.update_test_case_verification(c['id'],'supported','r',[],'cart',expected_case_fingerprint=reassess.case_fingerprint(original))
        self.assertEqual(db.list_test_case_assessments(self.repo_id,c['id']),[])
        self.assertEqual(db.list_test_cases(self.repo_id)[0]['expected_result'],'Human edit')

    @mock.patch('server.llm_test_generation.is_configured',return_value=True)
    def test_foreign_case_ids_are_rejected(self,_):
        response = self.client.post(f'/api/repos/{self.repo_id}/test-cases/reassess',json={'case_ids':['foreign-case']})
        self.assertEqual(response.status_code,404)
        self.assertEqual(self.client.get(f'/api/repos/other/test-cases/foreign-case/assessments').status_code,404)

    @mock.patch('server.llm_test_generation.is_configured',return_value=True)
    def test_selected_mixed_origins_use_existing_cases_and_same_worker(self,_):
        a = self.create_case()
        b = self.create_case(source_doc_id=self.doc_id)
        run = db.get_latest_successful_run(self.repo_id)
        expected_ids = {a['id'],b['id']}
        state = (None,[],None,{},[],run['id'])
        def fake_verify(repo,g,docs,root,context,index,cases,run_id):
            self.assertEqual({c['id'] for c in cases},expected_ids)
            return VerificationResult(results=[{'id':c['id']} for c in cases])
        with mock.patch.object(server_app,'_load_verification_run_state',return_value=state), mock.patch.object(server_app,'_verify_cases_for_document',side_effect=fake_verify), mock.patch('server.app.threading.Thread') as thread:
            def start():
                call = thread.call_args.kwargs
                call['target'](*call['args'])
            thread.return_value.start.side_effect = start
            response = self.client.post(f'/api/repos/{self.repo_id}/test-cases/reassess',json={'case_ids':list(expected_ids)})
        self.assertEqual(response.status_code,200)
        job = db.get_bulk_job(response.json()['id'])
        self.assertEqual(job['status'],'success')
        self.assertEqual(len(db.list_test_cases(self.repo_id)),2)

    def test_omitted_case_keeps_old_evidence_and_marks_job_failed(self):
        c = self.create_case()
        job = db.create_bulk_job(self.repo_id,'test_reassessment',1)
        run = db.get_latest_successful_run(self.repo_id)
        with mock.patch.object(server_app,'_load_verification_run_state',return_value=(None,[],None,{},[],run['id'])), mock.patch.object(server_app,'_verify_cases_for_document',return_value=VerificationResult(results=[])):
            server_app._reassessment_worker(job['id'],self.repo_id,[c['id']])
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'failed')
        self.assertEqual(db.list_test_case_assessments(self.repo_id,c['id']),[])

    def test_real_verifier_route_saves_three_way_report_and_invalidates_on_story_edit(self):
        db.update_document(self.doc_id, {'content':'Adding an item must return it.'})
        c = self.create_case(source_doc_id=self.doc_id)
        story = {'kind':'story','id':self.doc_id,'quote':'Adding an item must return it.'}
        code = {'kind':'code','id':'Cart.add','quote':'pass'}
        raw = {'context_sufficient':True,'missing_context':[], 'id':c['id'],'status':'contradicted','covers':['Cart.add'],'rationale':'Returns None',
               'requirement_alignment':{'status':'supported','rationale':'Expected return matches story','citations':[story]},
               'code_evidence':{'status':'contradicted','rationale':'pass returns None','citations':[code], 'conflict':{'left_quote':'Rejected','right_quote':'pass','difference':'The test expects rejection; the implementation returns None.'}},
               'implementation_alignment':{'status':'contradicted','rationale':'Story requires a returned item','citations':[story,code], 'conflict':{'left_quote':story['quote'],'right_quote':'pass','difference':'The story requires returning an item, but this body returns None.'}}}
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=json.dumps({'verifications':[raw]})))]
        sdk = mock.Mock()
        sdk.chat.completions.create.return_value = response
        with mock.patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}), mock.patch('openai.OpenAI',return_value=sdk), mock.patch('kg.embeddings.is_configured',return_value=False):
            result = self.client.post(self._document_verify_url())
        self.assertEqual(result.status_code,200,result.text)
        saved = db.list_test_cases(self.repo_id)[0]
        self.assertEqual(saved['reassessment']['outcome'],'potential_regression')
        self.assertTrue(saved['reassessment']['source_context']['files'])
        self.assertEqual(saved['status'],'contradicted')
        self.assertEqual(len(db.list_test_case_assessments(self.repo_id,c['id'])),1)
        self.assertFalse(self.client.get(f'/api/repos/{self.repo_id}/test-cases').json()[0]['is_stale'])
        db.update_document(self.doc_id,{'content':'Adding an item must return nothing.'})
        self.assertTrue(self.client.get(f'/api/repos/{self.repo_id}/test-cases').json()[0]['is_stale'])
        dashboard = self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()
        self.assertEqual(dashboard['test_cases']['code_coverage']['stale'],1)

    def _document_verify_url(self):
        return f'/api/repos/{self.repo_id}/documents/{self.doc_id}/test-cases/verify'
