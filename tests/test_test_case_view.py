import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient
from server import db, test_case_view as view
from server.app import app


class CaseViewTests(unittest.TestCase):
    def test_imported_origin_survives_code_and_story_links(self):
        case = {'created_via': 'imported', 'source_doc_id': 'story', 'module': 'm', 'covers': ['X'], 'status': 'supported'}
        self.assertEqual(view.filter_cases([case], source='imported'), [case])
        self.assertEqual(view.filter_cases([case], source='generated'), [])
        self.assertEqual(view.filter_cases([case], source='story'), [])

    def test_fresh_case_cannot_hide_stale_inferred_requirement(self):
        case = {'run_id': 'new', 'inferred_run_id': 'old', 'assessment_kind': 'inferred_from_requirement',
                'status': 'supported', 'covers': ['X']}
        enriched = view.enrich_cases([case], 'new')[0]
        self.assertEqual(view.assessment_state(enriched), 'stale')

    def test_missing_evidence_is_not_support(self):
        self.assertEqual(view.assessment_state({'status': 'supported', 'covers': []}), 'insufficient')
        self.assertEqual(view.assessment_state({'status': None}), 'unassessed')

    def test_combined_filters_preserve_their_scope(self):
        matching = {'title': 'Negative amount', 'category': 'edge_case', 'module': 'billing', 'status': 'contradicted',
                    'covers': ['X'], 'source_doc_id': 'story', 'created_via': 'generated'}
        other = {**matching, 'created_via': 'imported'}
        self.assertEqual(view.filter_cases([matching, other], source='story', module='billing', category='edge_case',
                                          story='story', q='NEGATIVE', assessment='conflict'), [matching])

    def test_regeneration_preserves_verified_imported_cases(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(db, 'DATA_DIR', Path(tmp)), \
             mock.patch.object(db, 'DB_PATH', Path(tmp) / 'kg.db'):
            db.init_db()
            repo = db.create_repo({'name': 'Preserve imported', 'source_type': 'local', 'local_path': tmp})
            template = {'title': 'Existing case', 'category': 'happy_path', 'steps': [], 'expected_result': 'Success', 'covers': ['X']}
            imported = db.create_test_case(repo['id'], 'billing', None, template)
            with db.get_conn() as conn:
                conn.execute("UPDATE test_cases SET created_via='imported', status='supported' WHERE id=?", (imported['id'],))
            db.replace_test_cases(repo['id'], 'billing', None, [{**template, 'title': 'Fresh generated case'}])
            rows = db.list_test_cases(repo['id'])
            self.assertEqual(len(rows), 2)
            self.assertTrue(any(c['id'] == imported['id'] and c['created_via'] == 'imported' and c['status'] == 'supported' for c in rows))

    def test_exports_match_origin_search_and_assessment_filters(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(db, 'DATA_DIR', Path(tmp)), \
             mock.patch.object(db, 'DB_PATH', Path(tmp) / 'kg.db'):
            db.init_db()
            repo = db.create_repo({'name': 'Export scope', 'source_type': 'local', 'local_path': tmp})
            generated = db.create_test_case(repo['id'], 'billing', None, {'title': 'Reject negative amount', 'category': 'edge_case',
                      'steps': [{'action': 'Submit negative amount', 'expected': 'Reject'}], 'expected_result': 'Reject',
                      'status': 'contradicted', 'covers': ['Charge'], 'rationale': 'Guard missing'})
            imported = db.create_test_case(repo['id'], 'billing', None, {'title': 'Reject negative amount imported', 'category': 'edge_case',
                      'steps': [{'action': 'Submit negative amount', 'expected': 'Reject'}], 'expected_result': 'Reject',
                      'status': 'contradicted', 'covers': ['Charge']})
            with db.get_conn() as conn:
                conn.execute("UPDATE test_cases SET created_via='imported' WHERE id=?", (imported['id'],))
            client = TestClient(app)
            base = f"/api/repos/{repo['id']}/test-cases/export"
            response = client.get(base, params={'format': 'json', 'source': 'imported', 'q': 'negative', 'assessment': 'conflict'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([c['id'] for c in response.json()['test_cases']], [imported['id']])
            self.assertEqual(response.json()['test_cases'][0]['created_via'], 'imported')
            for fmt in ('csv', 'md'):
                response = client.get(base, params={'format': fmt, 'source': 'generated'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('Reject negative amount', response.text)
                self.assertNotIn('Reject negative amount imported', response.text)
                self.assertIn('Origin: Generated', response.text)
