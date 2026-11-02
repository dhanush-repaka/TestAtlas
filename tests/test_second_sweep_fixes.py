"""Regressions for the eight second-sweep findings across their real paths."""
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests import test_readiness_gaps as fixtures
from tests import test_journey_requirements as journey_fixtures
from server import app as api,db,work_item_import,llm_journey_requirements as journeys
from server.test_case_reassessment import documents_fingerprint
from kg.repo_parser import parse_repo
from kg.dev_graph_builder import build_dev_graph
from kg.source_evidence import gather_source_evidence,enforce_context

DRAFT={'title':'Generated draft','category':'happy_path','steps':[{'action':'a','expected':'b'}],'expected_result':'b','covers':['Cart.add']}
CRITERION={'text':'x','type':'positive','status':'supported','covers':['Cart.add']}

class SecondSweepLifecycleTests(unittest.TestCase):
    setUp=fixtures.ReadinessGapTests.setUp
    tearDown=fixtures.ReadinessGapTests.tearDown

    def requirement(self):
        run=db.get_latest_successful_run(self.repo_id)
        db.replace_story_requirements(self.repo_id,self.doc_id,run['id'],[CRITERION])
        return db.list_story_requirements(self.repo_id)[0]

    def requirements(self):
        return self.client.get(f'/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements').json()

    def test_ai_evidence_snapshot_does_not_move_when_human_confirms(self):
        req=self.requirement()
        with db.get_conn() as conn:
            conn.execute("UPDATE story_requirements SET ai_status='unknown',status='unknown' WHERE id=?",(req['id'],))
        before=self.requirements()[0]
        response=self.client.post(f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm",json={'status':'supported','assessment_version':before['assessment_version']})
        self.assertEqual(response.status_code,200,response.text)
        after=self.requirements()[0]
        self.assertEqual(after['status'],'supported')
        self.assertEqual(before['ai_evidence_level'],after['ai_evidence_level'])
        self.assertEqual(after['ai_evidence_level'],'inconclusive')

    def test_older_run_cannot_be_confirmed_even_with_current_display_token(self):
        req=self.requirement()
        self.client.post(f'/api/repos/{self.repo_id}/run')
        shown=self.requirements()[0]
        self.assertTrue(shown['is_stale'])
        response=self.client.post(f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm",json={'status':'supported','assessment_version':shown['assessment_version']})
        self.assertEqual(response.status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_reanalysis_between_validation_and_write_blocks_confirmation(self):
        req=self.requirement();shown=self.requirements()[0]
        original=db.confirm_requirement_review
        def concurrent(*args,**kwargs):
            self.client.post(f'/api/repos/{self.repo_id}/run')
            return original(*args,**kwargs)
        with mock.patch.object(db,'confirm_requirement_review',side_effect=concurrent):
            response=self.client.post(f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm",json={'status':'supported','assessment_version':shown['assessment_version']})
        self.assertEqual(response.status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_inferred_case_inherits_document_staleness_and_export_filter(self):
        req=self.requirement()
        db.replace_test_cases(self.repo_id,'cart',req['run_id'],[DRAFT])
        db.update_document(self.doc_id,{'content':'Reject every item now'})
        case=self.client.get(f'/api/repos/{self.repo_id}/test-cases').json()[0]
        self.assertTrue(self.requirements()[0]['is_stale'])
        self.assertTrue(case['is_stale'])
        exported=self.client.get(f'/api/repos/{self.repo_id}/test-cases/export?format=json&assessment=stale').json()
        self.assertEqual(len(exported['test_cases']),1)

    def test_inferred_case_inherits_recorded_source_freshness(self):
        req=self.requirement()
        import hashlib
        file=Path(self.fixture_dir)/'cart.py'
        req_context={'files':[{'path':'cart.py','sha256':hashlib.sha256(file.read_bytes()).hexdigest()}]}
        db.replace_story_requirements(self.repo_id,self.doc_id,req['run_id'],[{**CRITERION,'source_context':req_context}])
        db.replace_test_cases(self.repo_id,'cart',req['run_id'],[DRAFT])
        file.write_text('class Cart:\n    def add(self,item):\n        raise ValueError("changed")\n')
        case=self.client.get(f'/api/repos/{self.repo_id}/test-cases').json()[0]
        self.assertTrue(case['is_stale'])
        self.assertEqual(case['source_freshness']['state'],'changed')

    def test_document_change_during_generation_preserves_suite_additive_and_replace(self):
        db.create_test_case(self.repo_id,None,None,{**DRAFT,'title':'Original'},source_doc_id=self.doc_id)
        def model(doc):
            db.update_document(self.doc_id,{'content':doc['content']+' changed'})
            return [DRAFT]
        for replace in ('true','false'):
            with self.subTest(replace=replace),mock.patch.object(api.llm_test_generation,'is_configured',return_value=True),mock.patch.object(api.llm_requirement_test_generation,'generate_test_cases_from_document',side_effect=model):
                response=self.client.post(f'/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements/generate-test-cases?replace={replace}')
            self.assertEqual(response.status_code,409)
            self.assertEqual([c['title'] for c in db.list_test_cases(self.repo_id)],['Original'])

    def test_bulk_generation_checks_document_revision_and_finishes_failed(self):
        db.create_test_case(self.repo_id,None,None,{**DRAFT,'title':'Original'},source_doc_id=self.doc_id)
        job=db.create_bulk_job(self.repo_id,'test_generation',1)
        def model(doc):
            db.update_document(self.doc_id,{'content':'Changed during model call'})
            return [DRAFT]
        with mock.patch.object(api.llm_requirement_test_generation,'generate_test_cases_from_document',side_effect=model):
            api._test_generation_bulk_worker(job['id'],self.repo_id,[self.doc_id],None,True)
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'failed')
        self.assertEqual([c['title'] for c in db.list_test_cases(self.repo_id)],['Original'])

    def test_generation_records_document_provenance_without_replacing_other_cases(self):
        doc=db.get_document(self.doc_id)
        created=db.replace_document_cases(self.repo_id,self.doc_id,None,[DRAFT],replace=False,expected_document_fingerprint=documents_fingerprint([doc]))
        stored=db.list_test_cases(self.repo_id)[0]
        self.assertEqual(stored['generation_context']['document_fingerprint'],documents_fingerprint([doc]))
        self.assertEqual(created[0]['module'],None)
        db.delete_document(self.doc_id)
        with self.assertRaisesRegex(RuntimeError,'deleted'):
            db.replace_document_cases(self.repo_id,self.doc_id,None,[DRAFT],replace=False,expected_document_fingerprint=documents_fingerprint([doc]))

    def test_derived_regeneration_keeps_identity_reviews_and_archives_old_judgment(self):
        run=db.get_latest_successful_run(self.repo_id)['id']
        db.replace_derived_requirements(self.repo_id,'journey',run,[CRITERION])
        req=db.list_story_requirements(self.repo_id)[0]
        db.create_test_case(self.repo_id,None,run,DRAFT,source_requirement_id=req['id'],source_journey_id='journey')
        from server.assessment_projection import requirement_version
        db.confirm_requirement_review(self.repo_id,req['id'],'supported','Intent confirmed',requirement_version(req,db.list_documents(self.repo_id)))
        db.replace_derived_requirements(self.repo_id,'journey',run,[CRITERION])
        rows=db.list_story_requirements(self.repo_id)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],req['id'])
        self.assertEqual(rows[0]['source'],'human_confirmed')
        self.assertEqual(db.list_human_reviews(self.repo_id)[0]['target_id'],req['id'])
        self.assertEqual(db.list_test_cases(self.repo_id)[0]['source_requirement_id'],req['id'])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requirement_assessments WHERE requirement_id=?',(req['id'],)).fetchone()[0],1)

    def test_removed_derived_criterion_is_retired_and_still_resolves(self):
        db.replace_derived_requirements(self.repo_id,'journey',None,[CRITERION])
        req=db.list_story_requirements(self.repo_id)[0]
        db.replace_derived_requirements(self.repo_id,'journey',None,[{**CRITERION,'text':'New criterion'}])
        self.assertEqual(len(db.list_story_requirements(self.repo_id)),1)
        self.assertEqual(db.get_story_requirement(req['id'])['retired'],1)

    def test_work_item_import_preserves_real_acceptance_criteria(self):
        file=io.StringIO();writer=csv.writer(file)
        writer.writerow(['ID','Title','Description','Acceptance Criteria'])
        writer.writerow(['123','Stock','Choose A | B\nKeep both','Reject over-available quantities'])
        response=self.client.post(f'/api/repos/{self.repo_id}/documents/import-work-items',files={'file':('stories.csv',file.getvalue().encode(),'text/csv')})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Choose A | B\nKeep both',response.json()[0]['content'])
        self.assertIn('Acceptance Criteria:\nReject over-available quantities',response.json()[0]['content'])

    def test_invalid_work_item_row_imports_nothing(self):
        before=len(db.list_documents(self.repo_id))
        response=self.client.post(f'/api/repos/{self.repo_id}/documents/import-work-items',files={'file':('stories.csv',b'ID,Title,Acceptance Criteria\n1,Good,AC\n2,Bad\n','text/csv')})
        self.assertEqual(response.status_code,400)
        self.assertEqual(len(db.list_documents(self.repo_id)),before)

class SecondSweepTransferTests(unittest.TestCase):
    def test_xlsx_per_sheet_headers_repeated_headers_and_pipes(self):
        import openpyxl
        workbook=openpyxl.Workbook();first=workbook.active
        first.append(['ID','Title','Acceptance Criteria']);first.append(['1','A','A | B']);first.append(['ID','Title','Acceptance Criteria'])
        second=workbook.create_sheet('Other');second.append(['Acceptance Criteria','Title','ID']);second.append(['Reject','B','2'])
        stream=io.BytesIO();workbook.save(stream);workbook.close()
        rows=work_item_import.parse_file('stories.xlsx',stream.getvalue())
        self.assertEqual([r['name'] for r in rows],['1: A','2: B'])
        self.assertIn('A | B',rows[0]['content']);self.assertIn('Reject',rows[1]['content'])

    def test_work_item_csv_large_cell_is_not_truncated(self):
        body='x'*310000
        rows=work_item_import.parse_file('stories.csv',f'ID,Title,Acceptance Criteria\n1,Story,{body}\n'.encode())
        self.assertIn(body,rows[0]['content'])

    def test_runtime_members_reassignment_and_missing_members_stay_unknown(self):
        for text in ('const service={}; service.reserve=globalThis.reserve; export function reserve(qty){return service.reserve(qty);}',
                     'const service={}; export function reserve(qty){return service.reserve(qty);}',
                     'const service={reserve(q){return q;}}; service.reserve=handler; export function reserve(q){return service.reserve(q);}'):
            with self.subTest(text=text),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'service.js').write_text(text)
                evidence=gather_source_evidence(build_dev_graph(parse_repo(root)),root,modules=['service'])
                self.assertFalse(evidence.manifest()['context_complete'])
                self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')

    def test_locally_implemented_object_methods_remain_available(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'service.js').write_text('const service={reserve(q){return q;}, add:(q)=>q}; export function reserve(q){return service.reserve(q)+service.add(q);}')
            evidence=gather_source_evidence(build_dev_graph(parse_repo(root)),root,modules=['service'])
            self.assertTrue(evidence.manifest()['context_complete'],evidence.limitations)

    def test_malformed_derived_requirements_fail_cleanly_and_preserve_valid_results(self):
        for covers in (7,{},'Cart.add',None):
            checked,reason=journeys._check_derived_requirement({**CRITERION,'covers':covers},{'Cart.add':'Cart.add'})
            self.assertIsNone(checked);self.assertIn('list',reason)
        for payload in ([],{'requirements':7},{'requirements':[{**CRITERION,'covers':7}]},{'requirements':[CRITERION,{'type':[]}]}):
            client=journey_fixtures.mock_model_response(payload)
            with self.subTest(payload=payload),mock.patch.dict(os.environ,{'OPENAI_API_KEY':'test'}),mock.patch('openai.OpenAI',return_value=client):
                with self.assertRaises(RuntimeError):
                    journeys.generate_journey_requirements('Journey',{'Cart.add':'pass'})
