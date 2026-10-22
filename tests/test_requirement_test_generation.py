"""Tests for generating test case(s) directly FROM a user story's own text
(server/llm_requirement_test_generation.py) -- ungrounded, no code shown, no
Story Coverage prerequisite -- its storage (server/db.py's create_test_case),
and the API routes in server/app.py.

The core safety property under test throughout: generation NEVER produces a
real citation -- `covers` is always `[]` coming out of the model, even when
the mocked response fabricates one, because generation never sees any code to
validate a citation against (canon={}). Grounding (status/covers/module) is a
later, separate step (server/app.py's document-scoped Verify, tested in
tests/test_document_test_verification.py), not something this module ever
claims to know.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import db
from server import llm_requirement_test_generation as rtg
from server.app import app

REQUIREMENT = {
    "id": "req-1", "text": "Users cannot checkout with an empty cart", "type": "negative",
}

DOC = {"name": "Checkout story", "content": "Users can add items to the cart. Users cannot checkout with an empty cart."}


def mock_model_response(payload):
    r = mock.Mock()
    r.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
    client = mock.Mock()
    client.chat.completions.create.return_value = r
    return client


class GenerateTestCasesFromRequirementTests(unittest.TestCase):
    def _run(self, test_cases_payload, requirement=None):
        requirement = requirement if requirement is not None else REQUIREMENT
        client = mock_model_response({"test_cases": test_cases_payload})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return rtg.generate_test_cases_from_requirement(requirement), client

    def test_a_case_is_stored_with_no_status_and_no_covers_even_if_the_model_claims_some(self):
        cases, _ = self._run([
            {"title": "Checkout refuses an empty cart", "category": "error_handling", "priority": 1,
             "steps": [{"action": "Attempt checkout with an empty cart", "expected": "Checkout is refused"}],
             "covers": ["checkout"], "status": "supported"},  # the model's own guess, must be ignored entirely
        ])
        self.assertEqual(len(cases), 1)
        self.assertIsNone(cases[0].get("status"))
        self.assertEqual(cases[0]["covers"], [])  # no code was ever shown -- any citation is rejected

    def test_a_simple_requirement_still_yields_exactly_one_case(self):
        cases, _ = self._run([
            {"title": "x", "category": "error_handling", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]},
        ])
        self.assertEqual(len(cases), 1)

    def test_more_than_the_safety_cap_is_truncated_not_rejected(self):
        payload = [
            {"title": f"case {i}", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]}
            for i in range(rtg.SCENARIO_SAFETY_CAP + 10)
        ]
        cases, _ = self._run(payload)
        self.assertEqual(len(cases), rtg.SCENARIO_SAFETY_CAP)

    def test_a_genuinely_large_requirement_can_yield_many_scenarios_not_capped_low(self):
        payload = [
            {"title": f"case {i}", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]}
            for i in range(12)
        ]
        cases, _ = self._run(payload)
        self.assertEqual(len(cases), 12)

    def test_prompt_contains_the_criterion_text_and_type_not_any_prior_judgment(self):
        _, client = self._run([{"title": "x", "category": "error_handling", "priority": 1,
                                 "steps": [{"action": "a", "expected": "b"}]}])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Users cannot checkout with an empty cart", prompt)
        self.assertIn("negative", prompt)
        self.assertNotIn("ALREADY-CONFIRMED", prompt)
        self.assertNotIn("REAL SOURCE CODE", prompt)
        self.assertIn("NOT shown the codebase", prompt)

    def test_prompt_instructs_boundary_value_analysis_for_numeric_ranges(self):
        # A range/threshold/limit criterion (a discount percentage, a coupon
        # amount) must get one case PER boundary, not one representative case
        # for the whole range -- that's the gap a real-world comparison against
        # ChatGPT surfaced: ours collapsed "valid percentage discount" into one
        # case where a thorough QA pass writes one per boundary (0%, typical,
        # 100%, just-over, far-over, fractional).
        _, client = self._run([{"title": "x", "category": "happy_path", "priority": 1,
                                 "steps": [{"action": "a", "expected": "b"}]}])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("boundary-value analysis", prompt.lower())
        self.assertIn("NOT artificial variations", prompt)

    def test_one_invalid_scenario_among_valid_ones_is_dropped_not_fatal(self):
        cases, _ = self._run([
            {"title": "ok one", "category": "error_handling", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]},
            {"category": "error_handling", "priority": 1, "steps": [{"action": "a", "expected": "b"}]},  # no title
        ])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["title"], "ok one")

    def test_every_scenario_invalid_is_rejected_with_a_clear_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run([{"category": "error_handling", "priority": 1, "steps": [{"action": "a", "expected": "b"}]}])
        self.assertIn("rejected", str(ctx.exception))

    def test_test_cases_not_a_list_is_rejected_cleanly(self):
        client = mock_model_response({"test_cases": "not a list"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_requirement(REQUIREMENT)
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_requirement(REQUIREMENT)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_no_criterion_text_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_requirement({"id": "r", "text": "  ", "type": "positive"})
        self.assertIn("no text to generate from", str(ctx.exception))

    def test_an_api_failure_is_surfaced_not_swallowed(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_requirement(REQUIREMENT)
        self.assertIn("rate limited", str(ctx.exception))


class GenerateTestCasesFromDocumentTests(unittest.TestCase):
    def _run(self, test_cases_payload, doc=None):
        doc = doc if doc is not None else DOC
        client = mock_model_response({"test_cases": test_cases_payload})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return rtg.generate_test_cases_from_document(doc), client

    def test_a_case_is_stored_with_no_status_and_no_covers_even_if_the_model_claims_some(self):
        cases, _ = self._run([
            {"title": "x", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}], "covers": ["Inventory.reserve"], "status": "supported"},
        ])
        self.assertIsNone(cases[0].get("status"))
        self.assertEqual(cases[0]["covers"], [])

    def test_a_large_set_of_scenarios_can_yield_many_cases_not_capped_low(self):
        payload = [
            {"title": f"case {i}", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]}
            for i in range(14)
        ]
        cases, _ = self._run(payload)
        self.assertEqual(len(cases), 14)

    def test_more_than_the_safety_cap_is_truncated_not_rejected(self):
        payload = [
            {"title": f"case {i}", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]}
            for i in range(rtg.SCENARIO_SAFETY_CAP + 10)
        ]
        cases, _ = self._run(payload)
        self.assertEqual(len(cases), rtg.SCENARIO_SAFETY_CAP)

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_document(DOC)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_empty_content_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with self.assertRaises(RuntimeError) as ctx:
                rtg.generate_test_cases_from_document({"name": "x", "content": "   "})
        self.assertIn("nothing to generate from", str(ctx.exception))

    def test_prompt_contains_the_story_name_and_content_not_any_prior_judgment(self):
        _, client = self._run([{"title": "x", "category": "happy_path", "priority": 1,
                                 "steps": [{"action": "a", "expected": "b"}]}])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn(DOC["name"], prompt)
        self.assertIn(DOC["content"], prompt)
        self.assertNotIn("ALREADY-JUDGED", prompt)
        self.assertNotIn("REAL SOURCE CODE", prompt)
        self.assertIn("NOT shown any code", prompt)

    def test_prompt_instructs_boundary_value_analysis_for_numeric_ranges(self):
        _, client = self._run([{"title": "x", "category": "happy_path", "priority": 1,
                                 "steps": [{"action": "a", "expected": "b"}]}])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("boundary-value analysis", prompt.lower())
        self.assertIn("NOT artificial variations", prompt)


class CreateTestCaseStorageTests(unittest.TestCase):
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

    def test_never_touches_other_existing_cases_for_the_same_module(self):
        db.replace_test_cases(self.repo["id"], "cart", None, [
            {"title": "existing one", "category": "happy_path", "steps": [], "expected_result": "x", "covers": []},
            {"title": "existing two", "category": "happy_path", "steps": [], "expected_result": "y", "covers": []},
        ])
        before = {c["title"] for c in db.list_test_cases(self.repo["id"])}

        db.create_test_case(self.repo["id"], "cart", None, {
            "title": "from a requirement", "category": "error_handling", "priority": 1,
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b", "covers": [],
        }, source_requirement_id="req-1")

        after = {c["title"] for c in db.list_test_cases(self.repo["id"])}
        self.assertEqual(after, before | {"from a requirement"})

    def test_source_doc_id_round_trips_independently_of_the_other_source_fields(self):
        created = db.create_test_case(self.repo["id"], None, None, {
            "title": "x", "category": "error_handling", "priority": 1,
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b", "covers": [],
        }, source_doc_id="doc-1")
        self.assertEqual(created["source_doc_id"], "doc-1")
        self.assertIsNone(created["source_requirement_id"])
        self.assertIsNone(created["source_journey_id"])
        self.assertIsNone(created["module"])

        row = next(c for c in db.list_test_cases(self.repo["id"]) if c["id"] == created["id"])
        self.assertEqual(row["source_doc_id"], "doc-1")
        self.assertIsNone(row["module"])
        self.assertIsNone(row["status"])

    def test_source_doc_id_and_source_requirement_id_can_both_be_set(self):
        # A per-requirement-generated case still belongs to one document --
        # document-scoped Verify needs source_doc_id to find it regardless of
        # which button triggered generation.
        created = db.create_test_case(self.repo["id"], None, None, {
            "title": "x", "category": "error_handling", "priority": 1,
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b", "covers": [],
        }, source_requirement_id="req-1", source_doc_id="doc-1")
        self.assertEqual(created["source_requirement_id"], "req-1")
        self.assertEqual(created["source_doc_id"], "doc-1")


class GenerateTestCaseFromRequirementApiTests(unittest.TestCase):
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
            "class Cart:\n    def add(self, item):\n        pass\n\n"
            "def checkout(cart):\n    return True\n\n"
            "def apply_discount(cart, code):\n    return cart\n"
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

    def _url(self, requirement_id):
        return f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements/{requirement_id}/generate-test-case"

    def test_404_for_an_unknown_requirement_id(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            r = self.client.post(self._url("does-not-exist"))
        self.assertEqual(r.status_code, 404)

    def test_400_when_no_key_configured(self):
        db.replace_story_requirements(self.repo_id, self.doc_id, None, [
            {"text": "x", "type": "positive", "status": "supported", "covers": ["checkout"]},
        ])
        req_id = db.list_story_requirements(self.repo_id, self.doc_id)[0]["id"]
        r = self.client.post(self._url(req_id))
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_success_with_no_citation_required_up_front(self):
        # Generation no longer needs a resolved citation before it can run --
        # even a requirement Story Coverage marked "unknown" with no covers
        # at all can still generate test cases.
        db.replace_story_requirements(self.repo_id, self.doc_id, None, [
            {"text": "Users cannot checkout with an empty cart", "type": "negative", "status": "unknown", "covers": []},
        ])
        req_id = db.list_story_requirements(self.repo_id, self.doc_id)[0]["id"]

        client = mock_model_response({"test_cases": [{
            "title": "Checkout refuses an empty cart", "category": "error_handling", "priority": 1,
            "steps": [{"action": "Attempt checkout with an empty cart", "expected": "Checkout is refused"}],
            "covers": ["checkout"], "status": "supported",  # both must be ignored -- unverified until Verify runs
        }]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            r = self.client.post(self._url(req_id))
        self.assertEqual(r.status_code, 200)
        cases = r.json()
        self.assertEqual(len(cases), 1)
        self.assertIsNone(cases[0]["module"])
        self.assertIsNone(cases[0].get("status"))
        self.assertEqual(cases[0]["covers"], [])
        self.assertEqual(cases[0]["source_requirement_id"], req_id)
        self.assertEqual(cases[0]["source_doc_id"], self.doc_id)

        listed = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        self.assertTrue(any(c["id"] == cases[0]["id"] for c in listed))


class GenerateTestCasesFromDocumentApiTests(unittest.TestCase):
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
        (Path(self.fixture_dir) / "inventory.py").write_text(
            "class Inventory:\n    def reserve(self, sku, qty):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        r = self.client.post(f"/api/repos/{self.repo_id}/documents", json={"name": "story", "content": DOC["content"]})
        self.doc_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _url(self):
        return f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements/generate-test-cases"

    def test_404_for_an_unknown_document(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            r = self.client.post(f"/api/repos/{self.repo_id}/documents/does-not-exist/story-requirements/generate-test-cases")
        self.assertEqual(r.status_code, 404)

    def test_400_when_no_key_configured(self):
        r = self.client.post(self._url())
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_success_with_zero_story_coverage_runs_ever_done(self):
        # No Story Coverage run, no story_requirements rows at all -- generation
        # works directly from the document's own raw content.
        self.assertEqual(db.list_story_requirements(self.repo_id, self.doc_id), [])

        client = mock_model_response({"test_cases": [
            {"title": "Add item to cart", "category": "happy_path", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}], "covers": ["Inventory.reserve"], "status": "supported"},
            {"title": "Checkout with empty cart is rejected", "category": "error_handling", "priority": 1,
             "steps": [{"action": "a", "expected": "b"}]},
        ]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            r = self.client.post(self._url())
        self.assertEqual(r.status_code, 200)
        cases = r.json()
        self.assertEqual(len(cases), 2)
        for c in cases:
            self.assertIsNone(c["module"])
            self.assertIsNone(c.get("status"))
            self.assertEqual(c["covers"], [])
            self.assertIsNone(c["source_requirement_id"])
            self.assertEqual(c["source_doc_id"], self.doc_id)

        listed_ids = {c["id"] for c in self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()}
        self.assertTrue({c["id"] for c in cases}.issubset(listed_ids))

    def test_generation_is_additive_by_default(self):
        client = mock_model_response({"test_cases": [{"title": "x", "category": "happy_path", "priority": 1, "steps": [{"action": "a", "expected": "b"}]}]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            self.client.post(self._url())
            self.client.post(self._url())
        cases = [c for c in self.client.get(f"/api/repos/{self.repo_id}/test-cases").json() if c["source_doc_id"] == self.doc_id]
        self.assertEqual(len(cases), 2)  # both generation passes kept -- no silent replace

    def test_replace_true_clears_this_documents_own_cases_before_storing_the_new_batch(self):
        client = mock_model_response({"test_cases": [{"title": "first pass", "category": "happy_path", "priority": 1, "steps": [{"action": "a", "expected": "b"}]}]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            self.client.post(self._url())

        other_doc = self.client.post(f"/api/repos/{self.repo_id}/documents", json={"name": "other", "content": "y"}).json()
        client2 = mock_model_response({"test_cases": [{"title": "other doc case", "category": "happy_path", "priority": 1, "steps": [{"action": "a", "expected": "b"}]}]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client2):
            self.client.post(f"/api/repos/{self.repo_id}/documents/{other_doc['id']}/story-requirements/generate-test-cases")

        client3 = mock_model_response({"test_cases": [{"title": "second pass", "category": "happy_path", "priority": 1, "steps": [{"action": "a", "expected": "b"}]}]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client3):
            r = self.client.post(self._url(), params={"replace": "true"})
        self.assertEqual(r.status_code, 200)

        all_cases = self.client.get(f"/api/repos/{self.repo_id}/test-cases").json()
        this_doc_titles = {c["title"] for c in all_cases if c["source_doc_id"] == self.doc_id}
        self.assertEqual(this_doc_titles, {"second pass"})  # "first pass" is gone
        other_doc_titles = {c["title"] for c in all_cases if c["source_doc_id"] == other_doc["id"]}
        self.assertEqual(other_doc_titles, {"other doc case"})  # a different document's cases are untouched


class DeleteTestCasesBySourceDocTests(unittest.TestCase):
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

    def test_only_deletes_this_documents_own_cases(self):
        db.create_test_case(self.repo["id"], None, None, {
            "title": "doc a", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id="doc-a")
        db.create_test_case(self.repo["id"], None, None, {
            "title": "doc b", "category": "happy_path", "steps": [{"action": "a", "expected": "b"}],
            "expected_result": "b", "covers": [],
        }, source_doc_id="doc-b")
        db.replace_test_cases(self.repo["id"], "cart", None, [
            {"title": "code generated", "category": "happy_path", "steps": [], "expected_result": "x", "covers": []},
        ])

        db.delete_test_cases_by_source_doc(self.repo["id"], "doc-a")

        titles = {c["title"] for c in db.list_test_cases(self.repo["id"])}
        self.assertEqual(titles, {"doc b", "code generated"})


if __name__ == "__main__":
    unittest.main()
