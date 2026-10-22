"""Tests for bulk-job DB CRUD (server/db.py's bulk_jobs table) and the bulk
Story Coverage / test-generation API routes in server/app.py -- the
corporate-scale counterpart to the per-document routes, exercised with the
actual LLM calls mocked out but the background daemon thread, the WAL-mode
concurrent DB access, and the poll-for-progress shape all real.
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


class BulkJobCrudTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
            mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db"),
        ]
        for p in self._patches:
            p.start()
        db.init_db()
        self.repo = db.create_repo({"name": "t", "source_type": "local", "local_path": "/tmp"})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_create_then_get_returns_a_running_job_with_zero_processed(self):
        job = db.create_bulk_job(self.repo["id"], "story_coverage", 3)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["total"], 3)
        self.assertEqual(job["processed"], 0)
        self.assertEqual(job["results"], [])
        self.assertEqual(db.get_bulk_job(job["id"]), job)

    def test_progress_updates_bump_processed_and_append_results_in_order(self):
        job = db.create_bulk_job(self.repo["id"], "story_coverage", 2)
        db.update_bulk_job_progress(job["id"], "Doc A", {
            "document_id": "d1", "document_name": "Doc A", "ok": True, "detail": "3 requirement(s)",
        })
        db.update_bulk_job_progress(job["id"], "Doc B", {
            "document_id": "d2", "document_name": "Doc B", "ok": False, "detail": "boom",
        })
        fetched = db.get_bulk_job(job["id"])
        self.assertEqual(fetched["processed"], 2)
        self.assertEqual(fetched["current_document_name"], "Doc B")
        self.assertEqual([r["document_name"] for r in fetched["results"]], ["Doc A", "Doc B"])
        self.assertEqual(fetched["results"][1]["ok"], False)

    def test_finish_sets_status_and_finished_at(self):
        job = db.create_bulk_job(self.repo["id"], "test_generation", 1)
        self.assertIsNone(job["finished_at"])
        db.finish_bulk_job(job["id"], "success")
        fetched = db.get_bulk_job(job["id"])
        self.assertEqual(fetched["status"], "success")
        self.assertIsNotNone(fetched["finished_at"])

    def test_get_unknown_job_returns_none(self):
        self.assertIsNone(db.get_bulk_job("does-not-exist"))

    def test_deleting_a_repo_with_a_bulk_job_and_a_journey_does_not_violate_a_foreign_key(self):
        # Both bulk_jobs and journeys declare FOREIGN KEY (repo_id) REFERENCES
        # repos(id), and get_conn() turns on PRAGMA foreign_keys -- delete_repo
        # has to clear every table with that FK before deleting the repo row
        # itself, or this raises sqlite3.IntegrityError instead of succeeding.
        db.create_bulk_job(self.repo["id"], "story_coverage", 1)
        db.replace_journeys(self.repo["id"], None, [{
            "id": "journey:checkout", "name": "Checkout", "prefix": "/checkout",
            "entry_point_ids": [], "node_ids": [],
        }])
        db.delete_repo(self.repo["id"])  # must not raise
        self.assertIsNone(db.get_repo(self.repo["id"]))


class BulkApiTests(unittest.TestCase):
    """End-to-end through the real routes -- only the LLM calls themselves
    are mocked. Proves run-all returns before the loop finishes (not a
    blocking request) and that polling the job id reflects real progress
    against real per-document DB writes, not just a counter."""

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
        (Path(self.fixture_dir) / "cart.py").write_text(
            "class Cart:\n"
            "    def add(self, item):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        self.doc_ids = []
        for name in ("Story A", "Story B"):
            r = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
                "name": name, "content": "Users can add items to the cart.",
            })
            self.doc_ids.append(r.json()["id"])

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _wait_for_job(self, path, timeout=5.0):
        deadline = time.monotonic() + timeout
        job = None
        while time.monotonic() < deadline:
            job = self.client.get(path).json()
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job at {path} never finished within {timeout}s: {job}")

    def test_run_all_without_a_key_is_rejected_not_silently_skipped(self):
        r = self.client.post(f"/api/repos/{self.repo_id}/story-coverage/run-all")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_run_all_returns_immediately_and_polling_shows_progress_to_completion(self):
        def fake_generate(doc, context, g, source_root, index_path):
            return mock.Mock(requirements=[
                {"text": "Users can add items to the cart", "type": "positive", "status": "supported", "covers": ["Cart.add"]},
            ], dropped=[])

        # The patch has to stay active for as long as the background worker
        # thread might still be running, not just for the POST call -- the
        # route returns almost instantly (it only starts the thread), so a
        # `with` scoped tightly around the POST would unpatch the real
        # generate_story_requirements before the thread ever reaches it,
        # silently falling through to a REAL (paid) OpenAI call instead.
        with mock.patch("server.llm_story_coverage.is_configured", return_value=True), \
             mock.patch("server.llm_story_coverage.generate_story_requirements", side_effect=fake_generate):
            r = self.client.post(f"/api/repos/{self.repo_id}/story-coverage/run-all")
            self.assertEqual(r.status_code, 200)
            job = r.json()
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["total"], 2)
            self.assertEqual(job["processed"], 0)  # the route itself must not block on the loop

            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/story-coverage/jobs/{job['id']}")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["processed"], 2)
        self.assertEqual(len(finished["results"]), 2)
        self.assertTrue(all(r["ok"] for r in finished["results"]))

        # The requirements really did land, per document -- not just a
        # progress counter ticking with nothing actually stored.
        reqs = self.client.get(f"/api/repos/{self.repo_id}/documents/{self.doc_ids[0]}/story-requirements").json()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["covers"], ["Cart.add"])

    def test_generate_all_documents_returns_immediately_and_completes(self):
        # Ungrounded generation needs no Story Coverage setup at all -- it
        # works straight from each document's own story text.
        def fake_generate_cases(doc):
            return [{
                "title": "Add to cart happy path", "category": "happy_path",
                "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": [],
            }]

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_requirement_test_generation.generate_test_cases_from_document", side_effect=fake_generate_cases):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/generate-all-documents")
            self.assertEqual(r.status_code, 200)
            job = r.json()
            self.assertEqual(job["total"], 2)
            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/jobs/{job['id']}")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["processed"], 2)
        self.assertTrue(all(r["ok"] for r in finished["results"]))

        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        self.assertEqual(len(cases), 2)  # one per document
        self.assertTrue(all(c["source_doc_id"] in self.doc_ids for c in cases))
        self.assertTrue(all(c["module"] is None for c in cases))  # unverified -- Verify resolves this later

    def test_replace_true_clears_each_documents_own_prior_cases_first(self):
        for doc_id in self.doc_ids:
            db.create_test_case(self.repo_id, None, None, {
                "title": "stale case", "category": "happy_path",
                "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": [],
            }, source_doc_id=doc_id)

        def fake_generate_cases(doc):
            return [{
                "title": "fresh case", "category": "happy_path",
                "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": [],
            }]

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_requirement_test_generation.generate_test_cases_from_document", side_effect=fake_generate_cases):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/generate-all-documents", params={"replace": "true"})
            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/jobs/{r.json()['id']}")
        self.assertEqual(finished["status"], "success")

        titles = {c["title"] for c in self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()}
        self.assertEqual(titles, {"fresh case"})  # "stale case" is gone from both documents

    def test_a_document_with_no_content_is_recorded_as_failed_not_aborted(self):
        # Only the second document has real content -- the first must show
        # up as a failed entry in the job's results, and the second must
        # still succeed, rather than the whole job stopping short.
        empty_doc_id = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
            "name": "Empty story", "content": "   ",
        }).json()["id"]

        def fake_generate_cases(doc):
            if not (doc.get("content") or "").strip():
                raise RuntimeError("this document has no content yet -- nothing to generate from")
            return [{
                "title": "Add to cart happy path", "category": "happy_path",
                "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": [],
            }]

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_requirement_test_generation.generate_test_cases_from_document", side_effect=fake_generate_cases):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/generate-all-documents")
            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/jobs/{r.json()['id']}")

        self.assertEqual(finished["status"], "success")  # the JOB completed, even though one document failed
        by_id = {res["document_id"]: res for res in finished["results"]}
        self.assertFalse(by_id[empty_doc_id]["ok"])
        self.assertIn("nothing to generate from", by_id[empty_doc_id]["detail"])
        self.assertTrue(by_id[self.doc_ids[0]]["ok"])
        self.assertTrue(by_id[self.doc_ids[1]]["ok"])

    def test_job_from_a_different_repo_is_not_found(self):
        r = self.client.post("/api/repos", json={"name": "other", "source_type": "local", "local_path": self.fixture_dir})
        other_repo_id = r.json()["id"]
        with mock.patch("server.llm_story_coverage.is_configured", return_value=True), \
             mock.patch("server.llm_story_coverage.generate_story_requirements",
                         return_value=mock.Mock(requirements=[], dropped=[])):
            r = self.client.post(f"/api/repos/{self.repo_id}/story-coverage/run-all")
            job_id = r.json()["id"]
            # Let the worker finish while the patch is still active -- leaving
            # it running past this `with` block would unpatch the real
            # generate_story_requirements out from under a still-live thread.
            self._wait_for_job(f"/api/repos/{self.repo_id}/story-coverage/jobs/{job_id}")
        r = self.client.get(f"/api/repos/{other_repo_id}/story-coverage/jobs/{job_id}")
        self.assertEqual(r.status_code, 404)


class VerifyAllDocumentsBulkApiTests(unittest.TestCase):
    """Bulk document-scoped Verify -- the counterpart to BulkApiTests for the
    new ungrounded-generation-then-verify flow. No OPENAI_API_KEY is ever set
    in the real environment here, so the embeddings retrieval step falls back
    to "every module" on its own (kg/embeddings.py's own is_configured()
    check) without needing a separate mock -- only the top-level
    llm_test_verification.generate_test_case_verifications call is mocked."""

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
        (Path(self.fixture_dir) / "cart.py").write_text(
            "class Cart:\n"
            "    def add(self, item):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        self.doc_ids = []
        for name in ("Story A", "Story B"):
            r = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
                "name": name, "content": "Users can add items to the cart.",
            })
            doc_id = r.json()["id"]
            self.doc_ids.append(doc_id)
            db.create_test_case(self.repo_id, None, None, {
                "title": f"Add to cart ({name})", "category": "happy_path",
                "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": [],
            }, source_doc_id=doc_id)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _wait_for_job(self, path, timeout=5.0):
        deadline = time.monotonic() + timeout
        job = None
        while time.monotonic() < deadline:
            job = self.client.get(path).json()
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job at {path} never finished within {timeout}s: {job}")

    def test_rejected_with_no_key(self):
        r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/verify-all-documents")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_rejected_without_a_successful_run(self):
        r = self.client.post("/api/repos", json={"name": "no-run", "source_type": "local", "local_path": self.fixture_dir})
        no_run_repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{no_run_repo_id}/documents", json={"name": "d", "content": "x"})
        with mock.patch("server.llm_test_generation.is_configured", return_value=True):
            r = self.client.post(f"/api/repos/{no_run_repo_id}/test-cases/verify-all-documents")
        self.assertEqual(r.status_code, 400)
        self.assertIn("no successful analysis run yet", r.json()["detail"])

    def test_verify_all_returns_immediately_and_completes(self):
        def fake_verify(cases, context, sources):
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in cases
            ], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/verify-all-documents")
            self.assertEqual(r.status_code, 200)
            job = r.json()
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["total"], 2)
            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/verify-jobs/{job['id']}")
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["processed"], 2)
        self.assertTrue(all(r["ok"] for r in finished["results"]))

        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        self.assertEqual(len(cases), 2)
        for c in cases:
            self.assertEqual(c["status"], "supported")
            self.assertEqual(c["covers"], ["Cart.add"])
            self.assertEqual(c["module"], "cart")  # resolved for the first time by Verify

    def test_a_document_with_no_cases_yet_is_recorded_as_failed_not_aborted(self):
        empty_doc_id = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
            "name": "No cases yet", "content": "x",
        }).json()["id"]

        def fake_verify(cases, context, sources):
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in cases
            ], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/verify-all-documents")
            finished = self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/verify-jobs/{r.json()['id']}")

        self.assertEqual(finished["status"], "success")
        by_id = {res["document_id"]: res for res in finished["results"]}
        self.assertFalse(by_id[empty_doc_id]["ok"])
        self.assertIn("no generated test cases yet", by_id[empty_doc_id]["detail"])
        self.assertTrue(by_id[self.doc_ids[0]]["ok"])
        self.assertTrue(by_id[self.doc_ids[1]]["ok"])

    def test_job_from_a_different_repo_is_not_found(self):
        r = self.client.post("/api/repos", json={"name": "other", "source_type": "local", "local_path": self.fixture_dir})
        other_repo_id = r.json()["id"]

        def fake_verify(cases, context, sources):
            return mock.Mock(results=[], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(f"/api/repos/{self.repo_id}/test-cases/verify-all-documents")
            job_id = r.json()["id"]
            self._wait_for_job(f"/api/repos/{self.repo_id}/test-cases/verify-jobs/{job_id}")
        r = self.client.get(f"/api/repos/{other_repo_id}/test-cases/verify-jobs/{job_id}")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
