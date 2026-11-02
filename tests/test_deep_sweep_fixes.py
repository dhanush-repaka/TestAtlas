"""Behavioural regressions for DS01–DS30, with the JS races tested separately."""
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient

from server import db,app as api,doc_extract,test_case_import,test_export,test_case_reassessment as triage
from server.assessment_projection import requirement_version
from server.llm_test_verification import VerificationResult,_check_verification
from server.llm_requirement_test_generation import GeneratedCases,generate_test_cases_from_document
from tests import test_readiness_gaps as readiness
from tests.generation_fixtures import setup
from kg.source_evidence import gather_source_evidence,enforce_context
from kg.dev_graph_builder import build_dev_graph
from kg.repo_parser import parse_repo


class DeepSweepLifecycleTests(unittest.TestCase):
    setUp=readiness.ReadinessGapTests.setUp
    tearDown=readiness.ReadinessGapTests.tearDown
    _make_case=readiness.ReadinessGapTests._make_case
    _make_requirement=readiness.ReadinessGapTests._make_requirement
    make_assessed_case=readiness.ReadinessGapTests.make_assessed_case
    case=readiness.ReadinessGapTests.case
    review=readiness.ReadinessGapTests.review

    def test_ds01_document_revision_invalidates_requirements_and_dashboard(self):
        req=self._make_requirement(status='supported',covers=['Cart.add'])
        db.update_document(self.doc_id,{'content':'Reject every item now'})
        rows=self.client.get(f'/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements').json()
        self.assertTrue(rows[0]['is_stale'])
        dashboard=self.client.get(f'/api/repos/{self.repo_id}/dashboard').json()
        self.assertEqual(dashboard['requirements']['evidence']['stale'],1)
        self.assertEqual(db.get_story_requirement(req['id'])['status'],'supported')

    def test_ds02_story_confirmation_is_version_bound_and_preserves_ai_snapshot(self):
        req=self._make_requirement(status='needs_human_input',covers=['Cart.add'])
        url=f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm"
        version=requirement_version(req,db.list_documents(self.repo_id))
        self.assertEqual(self.client.post(url,json={'status':'supported','assessment_version':None}).status_code,428)
        db.update_document(self.doc_id,{'content':'New business criteria'})
        self.assertEqual(self.client.post(url,json={'status':'supported','assessment_version':version}).status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])
        self.assertEqual(db.get_story_requirement(req['id'])['ai_status'],'needs_human_input')

    def test_ds03_document_race_is_rechecked_at_review_insert(self):
        c=self.make_assessed_case(statuses=('supported',)*3,raw_status='supported')
        record=db.record_human_review
        def concurrent(*args,**kwargs):
            db.update_document(self.doc_id,{'content':'Edited during review'})
            return record(*args,**kwargs)
        with mock.patch('server.db.record_human_review',side_effect=concurrent):
            response=self.review(c,'confirmed_supported')
        self.assertEqual(response.status_code,409)
        self.assertEqual(db.list_human_reviews(self.repo_id),[])

    def test_ds04_inferred_version_tracks_requirement_judgment(self):
        c=self._make_case(status='supported',covers=['Cart.add'])
        with db.get_conn() as conn:conn.execute('UPDATE test_cases SET status=NULL WHERE id=?',(c['id'],))
        req=self._make_requirement(status='supported',covers=['Cart.add'])
        before=self.case(c['id'])
        self.assertEqual(self.review(before,'rejected_ai_judgment',note='Old inferred finding').status_code,200)
        db.confirm_story_requirement(req['id'],'contradicted','Corrected intent')
        after=self.case(c['id'])
        self.assertNotEqual(before['assessment']['version'],after['assessment']['version'])
        self.assertEqual(after['review_state']['state'],'previous_assessment')

    def test_ds05_all_failed_entries_cannot_finish_success(self):
        job=db.create_bulk_job(self.repo_id,'test_generation',1)
        with mock.patch('server.llm_requirement_test_generation.generate_test_cases_from_document',side_effect=RuntimeError('Controlled error')):
            api._test_generation_bulk_worker(job['id'],self.repo_id,[self.doc_id],None,False)
        result=db.get_bulk_job(job['id'])
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['results'][0]['ok'])

    def test_ds06_omitted_model_cases_fail_document_job_without_overwriting_cases(self):
        c=db.create_test_case(self.repo_id,None,None,{'title':'Keep case','category':'happy_path','steps':[{'action':'Do','expected':'Result'}],'expected_result':'Result'},source_doc_id=self.doc_id)
        from kg.graph_io import load_graph
        from kg.doc_gaps import gap_analysis_context
        run=db.get_latest_successful_run(self.repo_id);g=load_graph(run['graph_path']);docs=db.list_documents(self.repo_id)
        state=(g,docs,Path(self.fixture_dir),gap_analysis_context(g,docs,require_docs=False),[],run['id'])
        job=db.create_bulk_job(self.repo_id,'test_verification',1)
        with mock.patch('server.app._load_verification_run_state',return_value=state),mock.patch('server.llm_test_verification.generate_test_case_verifications',return_value=VerificationResult(results=[],model='controlled')):
            api._document_verification_bulk_worker(job['id'],self.repo_id,[self.doc_id])
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'failed')
        self.assertIsNone(next(item for item in db.list_test_cases(self.repo_id) if item['id']==c['id'])['status'])

    def test_ds07_unexpected_worker_error_reaches_terminal_failure(self):
        job=db.create_bulk_job(self.repo_id,'test_verification',1)
        with mock.patch('server.app._load_verification_run_state',side_effect=sqlite3.OperationalError('Controlled lock error')):
            api._document_verification_bulk_worker(job['id'],self.repo_id,[self.doc_id])
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'failed')

    def test_ds08_failed_replacement_rolls_back_deletion_and_insertions(self):
        old=db.create_test_case(self.repo_id,None,None,{'title':'Edited case','category':'happy_path','steps':[],'expected_result':'Preserve this'},source_doc_id=self.doc_id)
        good={'title':'New','category':'happy_path','steps':[],'expected_result':'Result'}
        with self.assertRaises(sqlite3.IntegrityError):
            db.replace_document_cases(self.repo_id,self.doc_id,None,[good,{**good,'title':'Broken','expected_result':None}])
        rows=db.list_test_cases(self.repo_id)
        self.assertEqual([row['id'] for row in rows],[old['id']])
        self.assertEqual(rows[0]['expected_result'],'Preserve this')

    def test_ds09_stable_criterion_ids_and_archived_removed_links(self):
        req=self._make_requirement(status='unknown')
        db.replace_story_requirements(self.repo_id,self.doc_id,None,[{'text':'x','type':'positive','status':'supported','covers':[]}])
        self.assertEqual(db.list_story_requirements(self.repo_id)[0]['id'],req['id'])
        db.replace_story_requirements(self.repo_id,self.doc_id,None,[{'text':'Different criterion','type':'positive','status':'unknown','covers':[]}])
        self.assertTrue(db.get_story_requirement(req['id'])['retired'])
        self.assertTrue(db.get_story_requirement(req['id'])['text'])
        self.assertEqual(len(db.list_story_requirements(self.repo_id)),1)

    def test_ds10_unassessed_case_cannot_be_disputed(self):
        c=self.case(self._make_case()['id'])
        self.assertEqual(self.review(c,'rejected_ai_judgment',note='No judgment exists').status_code,400)
        self.assertEqual(self.review(c,'needs_more_info',note='Please clarify this draft').status_code,200)

    def test_ds11_startup_recovers_dead_jobs_but_keeps_live_jobs(self):
        job=db.create_bulk_job(self.repo_id,'test_generation',1)
        db.recover_interrupted_jobs()
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'running')
        with db.get_conn() as conn:conn.execute('UPDATE bulk_jobs SET owner_pid=NULL WHERE id=?',(job['id'],))
        db.recover_interrupted_jobs()
        self.assertEqual(db.get_bulk_job(job['id'])['status'],'failed')
        self.assertIn('Interrupted',db.get_bulk_job(job['id'])['error'])

    def test_ds19_password_gates_reads_and_public_mode_requires_opt_in(self):
        with mock.patch.dict(os.environ,{'TESTATLAS_PASSWORD':'synthetic-only','TESTATLAS_PUBLIC_READ_ONLY':''}):
            self.assertEqual(self.client.get(f'/api/repos/{self.repo_id}/documents').status_code,401)
            self.assertEqual(self.client.get('/static/styles.css').status_code,200)
            self.assertEqual(self.client.get('/api/system/config').status_code,200)
            with mock.patch.dict(os.environ,{'TESTATLAS_PUBLIC_READ_ONLY':'1'}):
                self.assertEqual(self.client.get(f'/api/repos/{self.repo_id}/documents').status_code,200)

    def test_ds20_invalid_office_files_return_actionable_client_errors(self):
        response=self.client.post(f'/api/repos/{self.repo_id}/documents/upload',files={'file':('bad.xlsx',b'broken','application/octet-stream')})
        self.assertEqual(response.status_code,400)
        self.assertIn('Could not read',response.text)

    def test_ds22_invalid_repository_updates_are_rejected(self):
        self.assertEqual(self.client.put(f'/api/repos/{self.repo_id}',json={'source_type':'not-a-source'}).status_code,400)
        self.assertEqual(db.get_repo(self.repo_id)['source_type'],'local')
        self.assertEqual(self.client.put(f'/api/repos/{self.repo_id}',json={'name':'Renamed'}).status_code,200)

    def test_ds23_duplicate_bulk_submissions_are_rejected_and_resumable(self):
        c=self._make_case(status='supported',covers=['Cart.add'])
        with mock.patch('server.llm_test_generation.is_configured',return_value=True),mock.patch('server.app.threading.Thread') as thread:
            url=f'/api/repos/{self.repo_id}/test-cases/reassess'
            first=self.client.post(url,json={'case_ids':[c['id']]})
            second=self.client.post(url,json={'case_ids':[c['id']]})
        self.assertEqual(first.status_code,200)
        self.assertEqual(second.status_code,409)
        self.assertEqual(thread.call_count,1)
        jobs=self.client.get(f'/api/repos/{self.repo_id}/jobs/active').json()
        self.assertEqual(jobs[0]['id'],first.json()['id'])

    def test_ds24_repository_with_dashboard_snapshot_deletes_transactionally(self):
        db.upsert_dashboard_snapshot(self.repo_id,'2026-11-02',{'fixture':True})
        self.assertEqual(self.client.delete(f'/api/repos/{self.repo_id}').status_code,200)
        self.assertIsNone(db.get_repo(self.repo_id))
        self.assertEqual(db.list_dashboard_snapshots(self.repo_id),[])

    def test_ds25_confirmed_criterion_regeneration_does_not_double_count(self):
        req=self._make_requirement(status='supported',covers=['Cart.add'])
        db.confirm_story_requirement(req['id'],'supported','Human finding')
        db.replace_story_requirements(self.repo_id,self.doc_id,None,[{'text':'x','type':'positive','status':'supported','covers':['Cart.add']}])
        rows=db.list_story_requirements(self.repo_id)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],req['id'])
        self.assertEqual(rows[0]['source'],'human_confirmed')

    def test_ds26_partial_generation_warns_without_changing_list_api_and_preserves_replace(self):
        good={'title':'Good','category':'happy_path','steps':[{'action':'Do','expected':'Result'}],'expected_result':'Result'}
        partial=GeneratedCases([good],['Navigation missing'],3)
        url=f'/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements/generate-test-cases'
        with mock.patch('server.llm_test_generation.is_configured',return_value=True),mock.patch('server.llm_requirement_test_generation.generate_test_cases_from_document',return_value=partial):
            added=self.client.post(url)
            self.assertEqual(added.status_code,200)
            self.assertIsInstance(added.json(),list)
            self.assertEqual(json.loads(added.headers['X-TestAtlas-Generation'])['rejected'],1)
            before=db.list_test_cases(self.repo_id)
            replaced=self.client.post(url+'?replace=true')
            self.assertEqual(replaced.status_code,502)
            self.assertEqual([row['id'] for row in db.list_test_cases(self.repo_id)],[row['id'] for row in before])


class DeepSweepTransferTests(unittest.TestCase):
    def test_ds12_ds13_unavailable_dynamic_dependencies_block_definitive_code_findings(self):
        fixtures={'service.js':'export function reserve(client) { return client.reserve(); }',
                  'service.py':'from pathlib import Path\ndef load(path):\n    return Path(path).read_text()\n'}
        for name,text in fixtures.items():
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/name).write_text(text)
                evidence=gather_source_evidence(build_dev_graph(parse_repo(root)),root,modules=['service'])
                self.assertFalse(evidence.manifest()['context_complete'])
                self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')

    def test_ds14_distinct_stock_roles_are_not_equivalent(self):
        self.assertFalse(triage.same_direction_release_claim('Available stock increases after release.','Reserved stock increases after release.'))
        self.assertTrue(triage.same_direction_release_claim('Stock increases after release.','Available stock increases when reserved stock is released.'))

    def test_ds15_csv_cell_boundaries_and_newlines_are_preserved(self):
        stream=io.StringIO();writer=csv.writer(stream)
        writer.writerow(['Title','Step Action','Step Expected']);writer.writerow(['Mode','Choose A | B\nthen continue','Selected | correctly'])
        cases,error=test_case_import.parse_file('cases.csv',stream.getvalue().encode())
        self.assertIsNone(error)
        self.assertEqual(cases[0]['steps'][0],{'action':'Choose A | B\nthen continue','expected':'Selected | correctly'})

    def test_ds16_worksheets_keep_headers_and_column_order(self):
        import openpyxl
        workbook=openpyxl.Workbook();a=workbook.active;a.append(['Title','Step Action','Step Expected']);a.append(['First','Do first','First expected'])
        b=workbook.create_sheet('Two');b.append(['Step Expected','Title','Step Action']);b.append(['Second expected','Second','Do second'])
        buffer=io.BytesIO();workbook.save(buffer);workbook.close()
        cases,error=test_case_import.parse_file('cases.xlsx',buffer.getvalue())
        self.assertIsNone(error);self.assertEqual([c['title'] for c in cases],['First','Second'])
        self.assertEqual(cases[1]['steps'][0]['expected'],'Second expected')

    def test_ds17_json_export_preserves_setup_and_exact_origin_ids(self):
        case={'title':'Case','category':'happy_path','steps':[{'action':'Open','expected':'Opened','kind':'setup'}],
              'source_requirement_id':'criterion','source_doc_id':'story','source_journey_id':'journey'}
        exported=json.loads(test_export.to_json([case],{}))['test_cases'][0]
        self.assertEqual(exported['steps'],case['steps'])
        for key in ('source_requirement_id','source_doc_id','source_journey_id'):self.assertEqual(exported[key],case[key])

    def test_ds18_malformed_model_collection_is_dropped(self):
        result,error=_check_verification({'id':'case','status':'supported','covers':7},{'case'},{})
        self.assertIsNone(result);self.assertIn('list',error)

    def test_ds20_large_valid_csv_field_is_supported(self):
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['Title','Step Action','Step Expected']);writer.writerow(['Big','A'*131073,'Expected'])
        cases,error=test_case_import.parse_file('big.csv',stream.getvalue().encode())
        self.assertIsNone(error);self.assertEqual(len(cases[0]['steps'][0]['action']),131073)

    def test_ds21_direct_and_suggested_story_filters_have_same_export_scope(self):
        from server.test_case_view import filter_cases
        case={'title':'Code case','related_requirements':[{'doc_id':'story'}]}
        self.assertEqual(filter_cases([case],story='story'),[case])

    def test_ds29_large_structured_import_is_not_truncated(self):
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['Title','Step Action','Step Expected'])
        for i in range(400):writer.writerow([f'Case {i}','A'*1000,'Expected'])
        cases,error=test_case_import.parse_file('large.csv',stream.getvalue().encode())
        self.assertIsNone(error);self.assertEqual(len(cases),400);self.assertEqual(cases[-1]['expected_result'],'Expected')
        self.assertTrue(test_case_import.parse_test_cases('Title | Action | Expected\n[...truncated -- marker...]')[1])

    def test_ds30_empty_scenarios_are_rejected_but_metadata_rows_are_allowed(self):
        self.assertTrue(test_case_import.parse_test_cases('Title | Step Action | Step Expected\nEmpty |  | ')[1])
        rows=[['Title','Step Action','Step Expected'],['Metadata','',''],['','Do','Result']]
        self.assertEqual(test_case_import.parse_rows([('Sheet',rows)])[0]['expected_result'],'Result')

    def test_ds26_generator_reports_rejected_candidates(self):
        good={'title':'Good','category':'happy_path','steps':[{'action':'Do','expected':'Result'}],'expected_result':'Result','setup':setup()}
        bad={**good,'setup':{'status':'needs_clarification','initial_state':'Unknown entry'}}
        response=mock.Mock(usage=None);response.choices=[mock.Mock(message=mock.Mock(content=json.dumps({'test_cases':[good,bad]})))]
        with mock.patch('openai.OpenAI') as sdk,mock.patch('server.llm_requirement_test_generation.is_configured',return_value=True):
            sdk.return_value.chat.completions.create.return_value=response
            cases=generate_test_cases_from_document({'name':'Story','content':'A scenario'})
        self.assertEqual(len(cases),1);self.assertEqual(cases.diagnostics['rejected'],1)
