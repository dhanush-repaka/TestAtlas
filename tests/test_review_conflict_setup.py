"""User regressions: visible disputes, unexplained conflicts, reproducible setup."""
import copy
import json
import os
import unittest
from unittest import mock

from server import test_case_reassessment as triage, test_case_view, test_case_setup
from server import llm_test_verification as verify, llm_test_generation as generation
from server import llm_requirement_test_generation as stories, llm_journey_test_generation as journeys
from tests import test_human_reviews as review_fixture
from tests.generation_fixtures import setup


class ConflictExplanationTests(unittest.TestCase):
    def setUp(self):
        self.quote='Given reserved stock, when released, the available quantity is increased accordingly.'
        self.context={'documents':[{'id':'inventory','content':self.quote}], 'modules':[]}
        self.case={'id':'case','title':'Release stock','preconditions':'A fresh instance has no stock.',
                   'steps':[{'action':'Release reserved stock','expected':'The available quantity is increased accordingly.'}],
                   'expected_result':'The available quantity is increased accordingly.'}

    def report(self, entry):
        return triage.validate_report({'requirement_alignment':entry},self.case,self.context,{})['requirement_alignment']

    def test_repeating_release_criterion_does_not_establish_a_conflict(self):
        finding=self.report({'status':'contradicted','rationale':'The criteria state that release increases stock.',
                             'citations':[{'kind':'story','id':'inventory','quote':self.quote}]})
        self.assertEqual(finding['status'],'unknown')
        self.assertEqual(finding['model_status'],'contradicted')
        self.assertIn('opposing expectations',finding['validation_note'])

    def test_identical_expectations_cannot_be_presented_as_opposed(self):
        quote=self.case['expected_result']
        self.context['documents'][0]['content']=quote
        finding=self.report({'status':'contradicted','citations':[{'kind':'story','id':'inventory','quote':quote}],
            'conflict':{'left_quote':quote,'right_quote':quote,'difference':'Claimed difference'}})
        self.assertEqual(finding['status'],'unknown')

    def test_invented_test_expectation_is_rejected(self):
        finding=self.report({'status':'contradicted','citations':[{'kind':'story','id':'inventory','quote':self.quote}],
            'conflict':{'left_quote':'Stock must decrease after release','right_quote':self.quote,'difference':'Decrease versus increase'}})
        self.assertEqual(finding['status'],'unknown')

    def test_valid_opposition_is_shown_with_both_exact_expectations(self):
        self.case['expected_result']='Stock decreases after release.'
        conflict={'left_quote':self.case['expected_result'],'right_quote':self.quote,'difference':'The test expects a decrease whereas the requirement specifies an increase.'}
        finding=self.report({'status':'contradicted','citations':[{'kind':'story','id':'inventory','quote':self.quote}],'conflict':conflict})
        self.assertEqual(finding['status'],'contradicted')
        self.assertEqual(finding['conflict'],conflict)

    def test_assessor_receives_preconditions_and_separate_comparison_rules(self):
        prompt=verify._build_prompt([self.case],self.context)
        self.assertIn(self.case['preconditions'],prompt)
        self.assertIn('reservation defect is not automatically a contradiction of a release criterion',prompt)
        self.assertIn('left_quote',prompt)

    def test_inventory_balance_error_is_separate_from_release_delta(self):
        # Arithmetic from the user-reported fresh-empty fixture; no AI inference.
        available=0
        available-=4
        before_release=available
        available+=2
        self.assertEqual(available-before_release,2)
        available-=1
        self.assertEqual(available,-3)
        self.assertNotEqual(available,1)


class SetupContractTests(unittest.TestCase):
    def setUp(self):
        self.case={'title':'Release stock','steps':[{'action':'Release 2 units','expected':'2 units become available'}],
                   'preconditions':None,'expected_result':'2 units become available','category':'happy_path'}

    def test_prefixes_entry_navigation_in_order_without_changing_expected_result(self):
        raw={'setup':{'status':'specified','starting_point':'Documented home page','initial_state':'Signed-out test user with an existing account',
             'steps':[{'action':'Open the documented home page','expected':'Home page appears'},
                      {'action':'Sign in with the test account','expected':'Signed-in home appears'},
                      {'action':'Open Inventory through the documented navigation','expected':'Inventory screen appears'}]}}
        before=copy.deepcopy(self.case)
        saved=test_case_setup.add_initial_setup(self.case,raw)
        self.assertEqual([s['action'] for s in saved['steps'][:3]],[s['action'] for s in raw['setup']['steps']])
        self.assertEqual(saved['steps'][-1],before['steps'][-1])
        self.assertEqual(saved['expected_result'],before['expected_result'])
        self.assertEqual(self.case,before)
        self.assertTrue(all(s['kind']=='setup' for s in saved['steps'][:3]))

    def test_library_setup_uses_fixture_values_without_inventing_login(self):
        raw={'setup':{'status':'specified','starting_point':'Inventory test fixture','initial_state':'SKU-A starts with 4 available units',
             'steps':[{'action':'Create an Inventory fixture with SKU-A stock set to 4','expected':'4 units are available before reservation'}]}}
        saved=test_case_setup.add_initial_setup(self.case,raw)
        self.assertIn('4 available',saved['preconditions'])
        self.assertNotIn('login',str(saved).lower())

    def test_missing_or_ambiguous_setup_is_rejected(self):
        for raw in ({},{'setup':{'status':'needs_clarification','initial_state':'Starting route not specified'}},{'setup':{**setup(),'steps':[]}}):
            with self.assertRaisesRegex(ValueError,'Setup needs clarification'):
                test_case_setup.add_initial_setup(self.case,raw)

    def test_all_generation_prompts_require_setup_and_disallow_invented_navigation(self):
        prompts=[generation._build_prompt({'module':'inventory','modules':[],'documents':[]}),
                 stories._build_prompt({'text':'Release increases stock','type':'positive'}),
                 stories._build_document_prompt({'name':'Inventory','content':'Release increases stock'}),
                 journeys._build_prompt('Inventory',[],{})]
        for prompt in prompts:
            self.assertIn('START EVERY CASE FROM A REPRODUCIBLE BEGINNING',prompt)
            self.assertIn('Do not invent a login',prompt)
            self.assertIn('setup',prompt)

    def test_setup_is_not_counted_as_behavioural_depth(self):
        saved=test_case_setup.add_initial_setup(self.case,{'setup':setup()})
        self.assertEqual(generation._thin([saved]),[saved])

    def test_generation_paths_reject_model_responses_without_setup(self):
        raw={'title':'Check stock boundary','category':'edge_case','steps':[{'action':'Release 2','expected':'Stock increases'}]}
        response=mock.Mock()
        response.choices=[mock.Mock(message=mock.Mock(content=json.dumps({'test_cases':[raw],'test_case':raw}))) ]
        sdk=mock.Mock()
        sdk.chat.completions.create.return_value=response
        with mock.patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}),mock.patch('openai.OpenAI',return_value=sdk):
            result=generation.generate_test_cases({'module':'inventory','modules':[],'documents':[]})
            self.assertEqual(result.cases,[])
            self.assertIn('Setup needs clarification',' '.join(result.dropped))
            for call in (lambda:stories.generate_test_cases_from_requirement({'text':'Release increases stock','type':'positive'}),
                         lambda:stories.generate_test_cases_from_document({'name':'Inventory','content':'Release increases stock'}),
                         lambda:journeys.generate_test_case_from_journey('Inventory',[{'text':'Release increases stock','type':'positive','status':'unknown','covers':[]}],{})):
                with self.assertRaisesRegex(RuntimeError,'Setup needs clarification'):
                    call()


class ReviewStateApiTests(unittest.TestCase):
    setUp=review_fixture.HumanReviewsApiTestCase.setUp
    tearDown=review_fixture.HumanReviewsApiTestCase.tearDown
    _make_case=review_fixture.HumanReviewsApiTestCase._make_case

    def test_disagree_is_visible_while_unknown_ai_status_is_preserved(self):
        case=self._make_case(status='unknown',covers=['Cart.add'])
        response=self.client.post(f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review",json={'decision':'rejected_ai_judgment','note':'The stated release expectations agree.'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['review_state']['state'],'disputed')
        cases=self.client.get(f'/api/repos/{self.repo_id}/test-cases').json()
        saved=next(c for c in cases if c['id']==case['id'])
        self.assertEqual(saved['status'],'unknown')
        self.assertEqual(saved['review_state']['label'],'Disputed by reviewer')
        self.assertEqual(test_case_view.filter_cases(cases,assessment='disputed'),[saved])

    def test_comments_required_for_disagree_and_information_requests(self):
        case=self._make_case(status='unknown',covers=['Cart.add'])
        url=f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review"
        for decision in ('rejected_ai_judgment','needs_more_info'):
            self.assertEqual(self.client.post(url,json={'decision':decision,'note':'  '}).status_code,400)
            response=self.client.post(url,json={'decision':decision,'note':'  Missing starting stock quantity.  '})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['note'],'Missing starting stock quantity.')

    def test_agree_with_uncertainty_preserves_ai_status_and_metrics(self):
        case=self._make_case(status='unknown',covers=['Cart.add'])
        url=f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review"
        self.assertEqual(self.client.post(url,json={'decision':'confirmed_supported'}).status_code,400)
        response=self.client.post(url,json={'decision':'agreed_uncertain'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['review_state']['label'],'Reviewer agrees with the uncertain assessment')
        saved=next(c for c in self.client.get(f'/api/repos/{self.repo_id}/test-cases').json() if c['id']==case['id'])
        self.assertEqual(saved['status'],'unknown')
        metrics=self.client.get(f'/api/repos/{self.repo_id}/review-accuracy').json()
        self.assertEqual(metrics['agreed_uncertain_count'],1)
        self.assertEqual(metrics['agreed_with_ai'],0)

    def test_unassessed_cases_cannot_be_agreed(self):
        case=self._make_case(status=None,covers=[])
        url=f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review"
        self.assertEqual(self.client.post(url,json={'decision':'agreed_uncertain'}).status_code,400)

    def test_review_from_an_older_assessment_is_not_a_current_dispute(self):
        state=test_case_view.review_state({'assessed_at':'2026-11-02T01:00:00+00:00'},
              {'decision':'rejected_ai_judgment','reviewed_at':'2026-11-02T00:00:00+00:00'})
        self.assertEqual(state['state'],'previous_assessment')


class GenerationPreservationApiTests(unittest.TestCase):
    from tests import test_document_test_verification as fixture
    setUp=fixture.VerifyCasesForDocumentApiTests.setUp
    tearDown=fixture.VerifyCasesForDocumentApiTests.tearDown

    def test_rejected_setup_does_not_replace_existing_module_cases(self):
        from server import db
        previous=db.create_test_case(self.repo_id,'cart',None,{'title':'Existing edited case','category':'happy_path','steps':[{'action':'Keep this action','expected':'Keep this result'}],'expected_result':'Keep this result'})
        with mock.patch.object(generation,'is_configured',return_value=True), mock.patch.object(generation,'generate_test_cases',return_value=generation.GenerationResult(cases=[],dropped=['Setup needs clarification'])):
            response=self.client.post(f'/api/repos/{self.repo_id}/test-cases/run?module=cart')
        self.assertEqual(response.status_code,502)
        self.assertIn('existing cases preserved',response.json()['detail'])
        self.assertEqual([c['id'] for c in db.list_test_cases(self.repo_id)],[previous['id']])
