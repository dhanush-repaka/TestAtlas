"""Cross-surface regressions for the reproduced readiness audit findings."""
import copy
import hashlib
import unittest
from pathlib import Path
from unittest import mock

from tests.test_human_reviews import HumanReviewsApiTestCase
from server import db, test_case_reassessment as triage
from server import test_export, source_freshness


class ReadinessGapTests(HumanReviewsApiTestCase):
    def make_assessed_case(self, *, statuses=('contradicted','supported','contradicted'), raw_status='unknown', with_hashes=True):
        case=self._make_case()
        story={'kind':'story','id':self.doc_id,'quote':'x'}
        code={'kind':'code','id':'Cart.add','quote':'pass'}
        raw={}
        for key,status in zip(triage.DIMENSIONS,statuses):
            citations=[story] if key=='requirement_alignment' else [code] if key=='code_evidence' else [story,code]
            raw[key]={'status':status,'rationale':'Controlled cross-surface finding','citations':citations}
            if status=='contradicted':
                raw[key]['conflict']={'left_quote':'x' if key=='implementation_alignment' else 'b',
                                     'right_quote':'x' if key=='requirement_alignment' else 'pass','difference':'Controlled differing expectations'}
        docs=db.list_documents(self.repo_id)
        report=triage.validate_report(raw,case,{'documents':docs,'modules':[{'module':'cart','files':[{'file':'cart.py','functions':[], 'classes':[{'name':'Cart','methods':['add']}]}]}]}, {'Cart.add':'class Cart:\n    def add(self,item):\n        pass'})
        if with_hashes:
            report['source_context']={'files':[{'path':'cart.py','sha256':hashlib.sha256((Path(self.fixture_dir)/'cart.py').read_bytes()).hexdigest()}]}
        # Mimic an older persisted overall status; projection must be authoritative.
        report['outcome']='needs_clarification' if raw_status=='unknown' else report['outcome']
        db.update_test_case_verification(case['id'],raw_status,'Stored historical status',['Cart.add'],'cart',reassessment=report)
        return self.case(case['id'])

    def case(self,case_id):
        return next(c for c in self.client.get(f'/api/repos/{self.repo_id}/test-cases').json() if c['id']==case_id)

    def review(self,case,decision,**extra):
        return self.client.post(f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review",json={
            'decision':decision,'assessment_version':case['assessment']['version'],**extra})

    def test_canonical_state_agrees_across_view_review_dashboard_and_all_exports(self):
        case=self.make_assessed_case()
        self.assertEqual(case['status'],'unknown')  # Historical data is retained.
        self.assertEqual(case['assessment']['state'],'conflict')
        self.assertEqual(case['assessment']['agreement_decision'],'confirmed_contradicted')
        review=self.review(case,'confirmed_contradicted',note='Reviewed opposing expectations')
        self.assertEqual(review.status_code,200,review.text)
        self.assertEqual(review.json()['ai_level_at_review'],'ai_conflict')
        self.assertEqual(review.json()['assessment_snapshot']['outcome'],'implementation_and_test_conflict')
        self.assertEqual(self.review(case,'agreed_uncertain').status_code,400)
        dashboard=self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()
        self.assertEqual(dashboard['test_cases']['evidence']['ai_conflict'],1)
        base=f'/api/repos/{self.repo_id}/test-cases/export'
        exported=self.client.get(base+'?format=json&assessment=conflict').json()['test_cases'][0]
        for key in ('status','expected_result','human_review','review_state','assessed_at','assessed_model','run_id','assessment','source_freshness'):
            self.assertIn(key,exported)
        self.assertEqual(exported['assessment']['version'],case['assessment']['version'])
        self.assertEqual(exported['human_review']['note'],'Reviewed opposing expectations')
        for fmt in ('csv','md'):
            content=self.client.get(base+f'?format={fmt}').text
            self.assertIn('Code and test conflict with requirements',content)
            self.assertIn('Reviewed opposing expectations',content)
            self.assertIn('Overall expected result',content)

    def test_legacy_unexplained_claim_is_inconclusive_on_every_surface(self):
        case=self.make_assessed_case(raw_status='contradicted')
        raw=next(c for c in db.list_test_cases(self.repo_id) if c['id']==case['id'])
        report=copy.deepcopy(raw['reassessment'])
        for key in triage.DIMENSIONS:
            report[key]['status']='contradicted'
            report[key]['conflict']=None
        db.update_test_case_verification(case['id'],'contradicted','Earlier claim',['Cart.add'],'cart',reassessment=report)
        case=self.case(case['id'])
        self.assertEqual(case['assessment']['state'],'inconclusive')
        self.assertEqual(case['assessment']['agreement_decision'],'agreed_uncertain')
        evidence=self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()['test_cases']['evidence']
        self.assertEqual(evidence['ai_conflict'],0)
        self.assertEqual(evidence['inconclusive'],1)
        self.assertEqual(self.review(case,'confirmed_contradicted').status_code,400)

    def test_missing_and_old_versions_are_rejected_without_saving_reviews(self):
        case=self.make_assessed_case()
        self.assertEqual(self.review(case,'confirmed_contradicted',assessment_version=None).status_code,428)
        db.update_test_case_verification(case['id'],'supported','Replaced',['Cart.add'],'cart')
        self.assertEqual(self.review(case,'confirmed_contradicted').status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_review_version_is_rechecked_atomically_at_insert(self):
        case=self.make_assessed_case()
        original=db.record_human_review
        def concurrent_write(*args,**kwargs):
            db.update_test_case_verification(case['id'],'supported','Changed after route read',['Cart.add'],'cart')
            return original(*args,**kwargs)
        with mock.patch('server.db.record_human_review',side_effect=concurrent_write):
            response=self.review(case,'confirmed_contradicted')
        self.assertEqual(response.status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_source_change_without_new_analysis_marks_stale_and_blocks_agreement(self):
        case=self.make_assessed_case(statuses=('supported',)*3,raw_status='supported')
        self.assertFalse(case['is_stale'])
        (Path(self.fixture_dir)/'cart.py').write_text('class Cart:\n    def add(self,item):\n        raise ValueError("changed")\n')
        changed=self.case(case['id'])
        self.assertTrue(changed['is_stale'])
        self.assertEqual(changed['source_freshness']['state'],'changed')
        self.assertEqual(self.review(changed,'confirmed_supported').status_code,409)
        for fmt in ('csv','md'):
            response=self.client.get(f'/api/repos/{self.repo_id}/test-cases/export?format={fmt}')
            self.assertIn('STALE',response.text)
        summary=self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()
        self.assertEqual(summary['test_cases']['evidence']['stale'],1)

    def test_disagreement_comment_and_freshness_survive_exports(self):
        case=self.make_assessed_case()
        self.assertEqual(self.review(case,'rejected_ai_judgment',note='The cited scenario is different.').status_code,200)
        case=self.case(case['id'])
        for fmt in ('csv','md','json'):
            response=self.client.get(f'/api/repos/{self.repo_id}/test-cases/export?format={fmt}&assessment=disputed')
            self.assertEqual(response.status_code,200)
            self.assertIn('The cited scenario is different.',response.text)
            self.assertIn('Disputed by reviewer',response.text)

    def test_uncertain_agreement_and_information_requests_do_not_lower_definitive_agreement(self):
        supported=self.make_assessed_case(statuses=('supported',)*3,raw_status='supported')
        uncertain=self.make_assessed_case(statuses=('unknown',)*3)
        request=self.make_assessed_case(statuses=('supported',)*3,raw_status='supported')
        self.assertEqual(self.review(supported,'confirmed_supported').status_code,200)
        self.assertEqual(self.review(uncertain,'agreed_uncertain').status_code,200)
        self.assertEqual(self.review(request,'needs_more_info',note='Need an integration trace').status_code,200)
        metrics=self.client.get(f'/api/repos/{self.repo_id}/review-accuracy').json()
        self.assertEqual(metrics['total_reviews'],3)
        self.assertEqual(metrics['definitive_review_count'],1)
        self.assertEqual(metrics['agreement_rate_pct'],100.0)
        self.assertEqual(metrics['agreed_uncertain_count'],1)
        self.assertEqual(metrics['needs_more_info_count'],1)

    def test_old_review_remains_history_after_reassessment(self):
        case=self.make_assessed_case()
        self.assertEqual(self.review(case,'confirmed_contradicted').status_code,200)
        db.update_test_case_verification(case['id'],'supported','Reassessment',['Cart.add'],'cart')
        self.assertEqual(self.case(case['id'])['review_state']['state'],'previous_assessment')
        review=db.list_human_reviews(self.repo_id)[0]
        self.assertEqual(review['assessment_version'],case['assessment']['version'])
        self.assertEqual(review['assessment_snapshot']['outcome'],'implementation_and_test_conflict')

    def test_all_27_combinations_share_review_filter_export_and_dashboard_state(self):
        import itertools
        from collections import Counter
        expected_counts=Counter()
        for statuses in itertools.product(('supported','contradicted','unknown'),repeat=3):
            with self.subTest(statuses=statuses):
                case=self.make_assessed_case(statuses=statuses)
                assessment=case['assessment']
                expected_counts[assessment['ai_level']]+=1
                response=self.review(case,assessment['agreement_decision'])
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['ai_level_at_review'],assessment['ai_level'])
                exported=self.client.get(f"/api/repos/{self.repo_id}/test-cases/export?format=json&assessment={assessment['state']}")
                self.assertEqual(exported.status_code,200)
                item=next(c for c in exported.json()['test_cases'] if c['id']==case['id'])
                self.assertEqual(item['assessment']['state'],assessment['state'])
                self.assertEqual(item['assessment']['label'],assessment['label'])
                self.assertEqual(item['assessment']['version'],assessment['version'])
        evidence=self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()['test_cases']['evidence']
        for level,count in expected_counts.items():
            self.assertEqual(evidence[level],count)

    def test_case_edit_invalidates_review_token_without_new_assessment(self):
        case=self.make_assessed_case()
        with db.get_conn() as conn:
            conn.execute('UPDATE test_cases SET expected_result=? WHERE id=?',('A changed expectation',case['id']))
        self.assertEqual(self.review(case,'rejected_ai_judgment',note='Old case text').status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_inferred_case_keeps_inference_label_and_raw_version(self):
        case=self._make_case(status='supported',covers=['Cart.add'])
        with db.get_conn() as conn:
            conn.execute('UPDATE test_cases SET status=NULL,reassessment=NULL WHERE id=?',(case['id'],))
        self._make_requirement(status='supported',covers=['Cart.add'])
        shown=self.case(case['id'])
        self.assertEqual(shown['assessment']['label'],'Inferred support')
        self.assertIsNone(shown['assessment']['agreement_decision'])
        self.assertEqual(self.review(shown,'rejected_ai_judgment',note='Shared citation is unrelated').status_code,200)

    def test_read_boundaries_never_fetch_remote_sources(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE repos SET source_type='github_git',github_owner='example',github_repo='example' WHERE id=?",(self.repo_id,))
        with mock.patch('server.runner.github_client.clone_or_update',side_effect=AssertionError('Read must not fetch')) as fetch:
            self.assertEqual(self.client.get(f'/api/repos/{self.repo_id}/test-cases').status_code,200)
            self.assertEqual(self.client.get(f'/api/repos/{self.repo_id}/dashboard').status_code,200)
            fetch.assert_not_called()


class SemanticAndFreshnessTests(unittest.TestCase):
    def test_same_direction_release_paraphrase_is_demoted_but_opposite_and_different_quantities_survive(self):
        case={'title':'Release stock','expected_result':'Stock increases after release.'}
        def check(quote,left=None):
            chosen={**case,'expected_result':left or case['expected_result']}
            raw={'requirement_alignment':{'status':'contradicted','citations':[{'kind':'story','id':'stock','quote':quote}],
                'conflict':{'left_quote':chosen['expected_result'],'right_quote':quote,'difference':'Claimed opposition'}}}
            return triage.validate_report(raw,chosen,{'documents':[{'id':'stock','content':quote}], 'modules':[]},{})
        report=check('Available stock increases when reserved stock is released.')
        self.assertEqual(report['requirement_alignment']['status'],'unknown')
        self.assertEqual(report['requirement_alignment']['semantic_verification'],'human_review_required')
        self.assertEqual(check('Stock decreases after release.')['requirement_alignment']['status'],'contradicted')
        self.assertEqual(check('Stock increases by 2 after release.','Stock increases by 1 after release.')['requirement_alignment']['status'],'contradicted')
        self.assertFalse(triage.same_direction_release_claim('Stock increases after release.','Stock does not increase after release.'))

    def test_crlf_hashes_match_the_original_text_evidence_and_deletions_are_stale(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root:
            path=Path(root)/'windows.py'
            path.write_bytes(b'def f():\r\n    return 1\r\n')
            expected=hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest()
            context={'files':[{'path':'windows.py','sha256':expected}]}
            self.assertEqual(source_freshness.check_source_context(context,root)['state'],'unchanged')
            path.unlink()
            self.assertEqual(source_freshness.check_source_context(context,root)['state'],'changed')

    def test_same_direction_guard_does_not_merge_distinct_subjects_or_conditions(self):
        self.assertFalse(triage.same_direction_release_claim('Stock for SKU-A increases after release.','Stock for SKU-B increases after release.'))
        self.assertFalse(triage.same_direction_release_claim('Stock increases after release.','Stock increases only after successful release.'))

    def test_unrecorded_source_is_explicit_and_path_escape_is_not_read(self):
        self.assertEqual(source_freshness.check_source_context(None,None)['state'],'unrecorded')
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root:
            with mock.patch('pathlib.Path.read_bytes',side_effect=AssertionError('Escaped path must not be read')):
                checked=source_freshness.check_source_context({'files':[{'path':'../outside','sha256':'bad'}]},root)
        self.assertEqual(checked['state'],'changed')
