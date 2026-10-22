"""Tests for document-scoped test-case Verify: kg/doc_gaps.py's
candidate_modules_context, server/app.py's retrieval-narrowing helpers
(_candidate_modules_for_cases), and the end-to-end route
(api_verify_test_cases_for_document). This is the "judge already-written,
ungrounded test cases against the real codebase" half of the pipeline --
generation itself is covered in tests/test_requirement_test_generation.py.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from kg.doc_gaps import candidate_modules_context
from kg.graph_io import load_graph
from server import app as server_app
from server import db
from server.app import app

CONTEXT_MODULES = [
    {"module": "cart", "description": "shopping cart", "files": [
        {"file": "cart.py", "functions": ["add"], "classes": [], "ui_text": []},
    ]},
    {"module": "checkout", "description": "checkout flow", "files": [
        {"file": "checkout.py", "functions": ["pay"], "classes": [], "ui_text": []},
    ]},
]


class CandidateModulesContextTests(unittest.TestCase):
    def _full_context(self, docs=None):
        import networkx as nx
        return candidate_modules_context(nx.MultiDiGraph(), docs or [], ["cart"])

    @mock.patch("kg.doc_gaps.gap_analysis_context")
    def test_narrows_to_the_requested_module_subset(self, mock_full):
        mock_full.return_value = {"ok": True, "documents": [], "modules": CONTEXT_MODULES}
        result = self._full_context()
        self.assertTrue(result["ok"])
        self.assertEqual([m["module"] for m in result["modules"]], ["cart"])

    @mock.patch("kg.doc_gaps.gap_analysis_context")
    def test_ok_false_when_none_of_the_candidates_exist_in_the_graph(self, mock_full):
        mock_full.return_value = {"ok": True, "documents": [], "modules": CONTEXT_MODULES}
        import networkx as nx
        result = candidate_modules_context(nx.MultiDiGraph(), [], ["does-not-exist"])
        self.assertFalse(result["ok"])

    @mock.patch("kg.doc_gaps.gap_analysis_context")
    def test_documents_pass_through_unchanged(self, mock_full):
        docs = [{"id": "d1", "name": "x", "content": "y"}]
        mock_full.return_value = {"ok": True, "documents": docs, "modules": CONTEXT_MODULES}
        result = self._full_context(docs)
        self.assertEqual(result["documents"], docs)

    @mock.patch("kg.doc_gaps.gap_analysis_context")
    def test_propagates_a_not_ok_full_context_unchanged(self, mock_full):
        mock_full.return_value = {"ok": False, "message": "no documents"}
        result = self._full_context()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "no documents")


class CandidateModulesForCasesTests(unittest.TestCase):
    CASES = [
        {"title": "Add to cart", "steps": [{"action": "add item", "expected": "in cart"}]},
        {"title": "Pay for order", "steps": [{"action": "pay", "expected": "paid"}]},
    ]

    def test_empty_index_falls_back_to_every_module(self):
        result = server_app._candidate_modules_for_cases([], ["cart", "checkout"], self.CASES)
        self.assertEqual(result, {"cart", "checkout"})

    @mock.patch("kg.embeddings.embed_texts")
    def test_embeddings_failure_falls_back_to_every_module(self, mock_embed):
        mock_embed.side_effect = RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
        index = [{"module": "cart", "embedding": [1.0, 0.0]}, {"module": "checkout", "embedding": [0.0, 1.0]}]
        result = server_app._candidate_modules_for_cases(index, ["cart", "checkout"], self.CASES)
        self.assertEqual(result, {"cart", "checkout"})

    @mock.patch("kg.embeddings.embed_texts")
    def test_a_populated_index_unions_each_cases_own_top_match(self, mock_embed):
        # First case's embedding points at "cart", second case's at "checkout" --
        # the union must contain both, not just whichever the LAST case retrieved.
        mock_embed.return_value = [[1.0, 0.0], [0.0, 1.0]]
        index = [{"module": "cart", "embedding": [1.0, 0.0]}, {"module": "checkout", "embedding": [0.0, 1.0]}]
        result = server_app._candidate_modules_for_cases(index, ["cart", "checkout"], self.CASES)
        self.assertEqual(result, {"cart", "checkout"})

    @mock.patch("kg.embeddings.embed_texts")
    def test_embedding_text_is_built_from_title_and_steps(self, mock_embed):
        mock_embed.return_value = [[1.0, 0.0]]
        server_app._candidate_modules_for_cases(
            [{"module": "cart", "embedding": [1.0, 0.0]}], ["cart"],
            [{"title": "Add to cart", "steps": [{"action": "add item", "expected": "in cart"}]}],
        )
        texts_arg = mock_embed.call_args.args[0]
        self.assertEqual(len(texts_arg), 1)
        self.assertIn("Add to cart", texts_arg[0])
        self.assertIn("add item", texts_arg[0])
        self.assertIn("in cart", texts_arg[0])


class VerifyCasesForDocumentApiTests(unittest.TestCase):
    """End-to-end through the real route -- OPENAI_API_KEY is never set in
    the real environment here, so kg/embeddings.py's own is_configured()
    check makes the retrieval step fall back to "every module" on its own
    (no separate embeddings mock needed); only the top-level
    llm_test_verification.generate_test_case_verifications call is mocked,
    same strategy as tests/test_bulk_jobs.py's VerifyAllDocumentsBulkApiTests."""

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
            "class Cart:\n    def add(self, item):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        r = self.client.post(f"/api/repos/{self.repo_id}/documents", json={"name": "story", "content": "x"})
        self.doc_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _url(self):
        return f"/api/repos/{self.repo_id}/documents/{self.doc_id}/test-cases/verify"

    def test_rejected_with_no_key(self):
        r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_rejected_when_the_document_has_no_cases_yet(self):
        with mock.patch("server.llm_test_generation.is_configured", return_value=True):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("generate some first", r.json()["detail"])

    def test_rejected_without_a_successful_run(self):
        r = self.client.post("/api/repos", json={"name": "no-run", "source_type": "local", "local_path": self.fixture_dir})
        no_run_repo_id = r.json()["id"]
        d = self.client.post(f"/api/repos/{no_run_repo_id}/documents", json={"name": "d", "content": "x"}).json()
        db.create_test_case(no_run_repo_id, None, None, {
            "title": "x", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id=d["id"])
        with mock.patch("server.llm_test_generation.is_configured", return_value=True):
            r = self.client.post(f"/api/repos/{no_run_repo_id}/documents/{d['id']}/test-cases/verify")
        self.assertEqual(r.status_code, 400)
        self.assertIn("no successful analysis run yet", r.json()["detail"])

    def test_success_resolves_status_covers_and_module_for_the_first_time(self):
        created = db.create_test_case(self.repo_id, None, None, {
            "title": "Add an item", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id=self.doc_id)
        self.assertIsNone(created["module"])

        def fake_verify(cases, context, sources):
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in cases
            ], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["applied"], 1)

        row = next(c for c in body["test_cases"] if c["id"] == created["id"])
        self.assertEqual(row["status"], "supported")
        self.assertEqual(row["covers"], ["Cart.add"])
        self.assertEqual(row["module"], "cart")  # resolved for the first time

    def test_a_case_the_model_never_addresses_stays_unverified(self):
        addressed = db.create_test_case(self.repo_id, None, None, {
            "title": "Addressed", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id=self.doc_id)
        unaddressed = db.create_test_case(self.repo_id, None, None, {
            "title": "Unaddressed", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id=self.doc_id)

        def fake_verify(cases, context, sources):
            # Only ever addresses the one named "Addressed" -- mirrors
            # generate_test_case_verifications' own real behavior of simply
            # omitting a case the model doesn't mention.
            addressed_cases = [c for c in cases if c["title"] == "Addressed"]
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in addressed_cases
            ], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 200)

        cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        by_id = {c["id"]: c for c in cases}
        self.assertEqual(by_id[addressed["id"]]["status"], "supported")
        self.assertIsNone(by_id[unaddressed["id"]]["status"])
        self.assertIsNone(by_id[unaddressed["id"]]["module"])

    def test_re_running_verify_is_idempotent(self):
        created = db.create_test_case(self.repo_id, None, None, {
            "title": "x", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id=self.doc_id)

        def fake_verify(cases, context, sources):
            return mock.Mock(results=[
                {"id": c["id"], "status": "supported", "covers": ["Cart.add"], "rationale": "r"} for c in cases
            ], dropped=[])

        with mock.patch("server.llm_test_generation.is_configured", return_value=True), \
             mock.patch("server.llm_test_verification.generate_test_case_verifications", side_effect=fake_verify):
            first = self.client.post(self._url()).json()
            second = self.client.post(self._url()).json()

        row1 = next(c for c in first["test_cases"] if c["id"] == created["id"])
        row2 = next(c for c in second["test_cases"] if c["id"] == created["id"])
        self.assertEqual(row1["status"], row2["status"])
        self.assertEqual(row1["covers"], row2["covers"])
        self.assertEqual(row1["module"], row2["module"])
        # Still exactly one row -- re-verifying patches in place, never duplicates.
        self.assertEqual(len(self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()), 1)


if __name__ == "__main__":
    unittest.main()
