"""End-to-end tests for the two new routes server/app.py adds for imported test
cases: POST /repos/{repo_id}/test-cases/import (server/test_case_import.py) and
POST /repos/{repo_id}/test-cases/verify-imported (reuses
server/llm_test_verification.py unchanged). Mirrors
tests/test_document_test_verification.py's VerifyCasesForDocumentApiTests setup
and mocking strategy for the verify route.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import db
from server.app import app

CSV_TEXT = (
    "ID,Work Item Type,Title,Test Step,Step Action,Step Expected,Priority,Description,Tags,Area Path,State\r\n"
    ',Test Case,Add to cart,1,Click add,Item appears in cart,2,Checks the add flow,"Shopping Cart; Happy path; cart",,Design\r\n'
)


class ImportTestCasesApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
            mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db"),
        ]
        for p in self._patches:
            p.start()
        db.init_db()
        self.client = TestClient(app)
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": "/tmp"})
        self.repo_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _url(self):
        return f"/api/repos/{self.repo_id}/test-cases/import"

    def test_404_for_an_unknown_repo(self):
        r = self.client.post("/api/repos/does-not-exist/test-cases/import", files={"file": ("x.csv", b"a,b\r\n1,2\r\n", "text/csv")})
        self.assertEqual(r.status_code, 404)

    def test_a_well_formed_csv_imports_successfully(self):
        r = self.client.post(self._url(), files={"file": ("cases.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
        self.assertEqual(r.status_code, 200)
        created = r.json()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], "Add to cart")
        self.assertEqual(created[0]["category"], "happy_path")
        self.assertEqual(created[0]["priority"], 2)

        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["created_via"], "imported")
        self.assertIsNone(cases[0]["module"])  # resolved later by Verify

    def test_a_file_with_no_recognizable_shape_is_rejected_with_a_clear_message(self):
        bad_csv = b"Summary,Owner\r\nNot a test case export,someone\r\n"
        r = self.client.post(self._url(), files={"file": ("notes.csv", bad_csv, "text/csv")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Title", r.json()["detail"])

    def test_importing_adds_to_existing_cases_rather_than_replacing_them(self):
        db.create_test_case(self.repo_id, "cart", None, {
            "title": "Already generated", "category": "happy_path",
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b", "covers": [],
        })
        self.client.post(self._url(), files={"file": ("cases.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        self.assertEqual(len(cases), 2)
        by_origin = {c["created_via"] for c in cases}
        self.assertEqual(by_origin, {"generated", "imported"})


class VerifyImportedTestCasesApiTests(unittest.TestCase):
    """Same mocking strategy as VerifyCasesForDocumentApiTests in
    tests/test_document_test_verification.py: OPENAI_API_KEY is never set
    here, so retrieval falls back to "every module" on its own; only
    generate_test_case_verifications is mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
            mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db"),
        ]
        for p in self._patches:
            p.start()
        db.init_db()
        self.client = TestClient(app)
        self.fixture_dir = tempfile.mkdtemp()
        (Path(self.fixture_dir) / "cart.py").write_text("class Cart:\n    def add(self, item):\n        pass\n")
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _url(self):
        return f"/api/repos/{self.repo_id}/test-cases/verify-imported"

    def test_rejected_with_no_key(self):
        r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_rejected_when_there_are_no_imported_cases(self):
        db.create_test_case(self.repo_id, "cart", None, {
            "title": "Generated, not imported", "category": "happy_path",
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b", "covers": [],
        })
        with mock.patch("server.llm_test_generation.is_configured", return_value=True):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("no imported test cases", r.json()["detail"])

    def test_rejected_without_a_successful_run(self):
        db.import_test_cases(self.repo_id, [{
            "title": "x", "category": "unclassified", "priority": None, "description": "",
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b",
        }])
        with mock.patch("server.llm_test_generation.is_configured", return_value=True):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("no successful analysis run yet", r.json()["detail"])

    def test_success_resolves_status_covers_and_module_for_imported_cases(self):
        self.client.post(f"/api/repos/{self.repo_id}/run")
        created = db.import_test_cases(self.repo_id, [{
            "title": "Add an item", "category": "unclassified", "priority": None, "description": "",
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b",
        }])[0]
        self.assertIsNone(created["module"])

        def fake_verify(cases, context, sources):
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in cases
            ], dropped=[], model="gpt-4o-mini")

        # The POST only starts a background thread and returns immediately --
        # the mocks must stay active for the whole polling loop too, not just
        # the POST call itself, or the background thread ends up calling the
        # REAL (unmocked) generate_test_case_verifications once the `with`
        # block here exits and reverts the patch out from under it.
        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(self._url())
            self.assertEqual(r.status_code, 200)
            job_id = r.json()["id"]

            job = None
            for _ in range(50):
                job = self.client.get(f"/api/repos/{self.repo_id}/test-cases/verify-jobs/{job_id}").json()
                if job["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(job["status"], "success")

        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        row = next(c for c in cases if c["id"] == created["id"])
        self.assertEqual(row["status"], "supported")
        self.assertEqual(row["covers"], ["Cart.add"])
        self.assertEqual(row["module"], "cart")  # resolved for the first time
        self.assertEqual(row["created_via"], "imported")  # unchanged by verification


if __name__ == "__main__":
    unittest.main()
