"""Local pilot lifecycle using real parsing/storage/routes and mocked AI only."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient
from server import db
from server.app import app


class PilotWorkflowTests(unittest.TestCase):
    def test_import_assess_review_change_reassess_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            code = source / 'cart.py'
            code.write_text('class Cart:\n    def add(self, item):\n        return item\n')
            with mock.patch.object(db, 'DATA_DIR', root / 'data'), \
                 mock.patch.object(db, 'DB_PATH', root / 'data/kg.db'), \
                 mock.patch.object(db, 'RUNS_DIR', root / 'data/runs'), \
                 mock.patch('server.llm_test_generation.is_configured', return_value=True), \
                 mock.patch('kg.embeddings.is_configured', return_value=False):
                db.init_db()
                client = TestClient(app)
                repo = client.post('/api/repos', json={'name': 'Pilot', 'source_type': 'local', 'local_path': str(source)}).json()
                base = f"/api/repos/{repo['id']}"
                first = client.post(base + '/run').json()
                self.assertEqual(first['status'], 'success')
                doc = client.post(base + '/documents', json={'name': 'Cart story', 'content': 'Users can add an item.'}).json()
                docbase = base + f"/documents/{doc['id']}"
                posted = client.post(docbase + '/story-requirements', json={'requirements': [
                    {'text': 'Users can add an item', 'type': 'positive', 'status': 'supported', 'covers': ['Cart.add'], 'rationale': 'Returns item'}]})
                self.assertEqual(posted.status_code, 200, posted.text)
                csv = 'ID,Work Item Type,Title,Test Step,Step Action,Step Expected\r\n,Test Case,Add item,1,Add an item,Item returned\r\n'
                imported = client.post(base + '/test-cases/import', files={'file': ('cases.csv', csv, 'text/csv')})
                self.assertEqual(imported.status_code, 200, imported.text)
                case_id = imported.json()[0]['id']
                def verify(cases, context, sources):
                    return mock.Mock(results=[{'id': c['id'], 'status': 'supported', 'covers': ['Cart.add'], 'rationale': 'Fixture assessment'} for c in cases], dropped=[], model='mock-pilot')
                # Exercise the shared verifier through document route with a real imported case.
                with db.get_conn() as conn:
                    conn.execute('UPDATE test_cases SET source_doc_id=? WHERE id=?', (doc['id'], case_id))
                with mock.patch('server.llm_test_verification.generate_test_case_verifications', side_effect=verify):
                    checked = client.post(docbase + '/test-cases/verify')
                    self.assertEqual(checked.status_code, 200, checked.text)
                    before = client.get(base + '/dashboard').json()
                    self.assertEqual(before['test_cases']['code_coverage']['fresh_linked'], 1)
                    reviewed = client.post(base + f'/test-cases/{case_id}/review', json={'decision': 'confirmed_contradicted', 'note': 'Independent disagreement'})
                    self.assertEqual(reviewed.status_code, 200, reviewed.text)
                    self.assertEqual(client.get(base + '/review-accuracy').json()['false_support_count'], 1)
                    code.write_text('class Cart:\n    def add(self, item):\n        return None\n')
                    second = client.post(base + '/run').json()
                    self.assertEqual(second['status'], 'success')
                    stale = client.get(base + '/dashboard').json()
                    self.assertEqual(stale['test_cases']['code_coverage']['stale'], 1)
                    self.assertIsNone(stale['test_cases']['code_coverage']['pct'])
                    self.assertEqual(stale['documents']['by_health']['needs_reassessment'], 1)
                    reassessed = client.post(docbase + '/test-cases/verify')
                    self.assertEqual(reassessed.status_code, 200, reassessed.text)
                    current = client.get(base + '/dashboard').json()
                    self.assertEqual(current['test_cases']['code_coverage']['stale'], 0)
                    self.assertEqual(db.list_test_cases(repo['id'])[0]['run_id'], second['id'])
                for fmt in ('json', 'csv', 'md'):
                    exported = client.get(base + '/test-cases/export', params={'format': fmt})
                    self.assertEqual(exported.status_code, 200, exported.text)
                    self.assertIn('Add item', exported.text)
