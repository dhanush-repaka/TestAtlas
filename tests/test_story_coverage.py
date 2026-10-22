"""Tests for Story Coverage (server/llm_story_coverage.py, its storage in
server/db.py, and the API routes in server/app.py). GenerateStoryRequirementsTests
mocks the OpenAI client (same style as tests/test_test_generation.py's
RunGenerationTests) to exercise generate_story_requirements() end-to-end --
prompt shape, response parsing, and validation all wired together -- without
a real API key or network call. Every other test class checks what WE
control directly: validation, the never-cite-a-fake-name guarantee, the
replace-scoped-to-one-document contract, and the tested-status cross-reference.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import networkx as nx
from fastapi.testclient import TestClient

from kg import embeddings
from server import db
from server import llm_story_coverage as sc
from server.app import app

CONTEXT = {
    "documents": [],
    "modules": [{
        "module": "cart",
        "files": [{
            "file": "cart", "purpose": None,
            "classes": [{"name": "Cart", "methods": ["add", "remove"]}],
            "functions": ["checkout"],
        }],
    }],
}
CANON = sc.known_code_names(CONTEXT)
DOC = {"name": "Checkout story", "content": "Users can add items to the cart. Users cannot checkout with an empty cart."}


def req(**over):
    base = {"text": "Users can add items to the cart", "type": "positive",
            "status": "supported", "covers": ["Cart.add"], "rationale": "found it"}
    return {**base, **over}


class CheckRequirementTests(unittest.TestCase):
    def test_a_real_citation_keeps_its_verdict(self):
        checked, why = sc._check_requirement(req(), CANON)
        self.assertIsNone(why)
        self.assertEqual(checked["status"], "supported")
        self.assertEqual(checked["covers"], ["Cart.add"])

    def test_a_fake_citation_demotes_supported_to_unknown(self):
        # This is the actual enforcement of "never a bare guess" -- a verdict
        # with no real evidence behind it is not trusted, even if the model
        # said "supported".
        checked, why = sc._check_requirement(req(covers=["NotReal.fake"]), CANON)
        self.assertIsNone(why)
        self.assertEqual(checked["status"], "unknown")
        self.assertEqual(checked["covers"], [])

    def test_contradicted_is_demoted_the_same_way(self):
        checked, _ = sc._check_requirement(req(status="contradicted", covers=["invented"]), CANON)
        self.assertEqual(checked["status"], "unknown")

    def test_unknown_status_needs_no_citation_at_all(self):
        checked, _ = sc._check_requirement(req(status="unknown", covers=[]), CANON)
        self.assertEqual(checked["status"], "unknown")

    def test_a_module_path_prefixed_citation_still_resolves(self):
        checked, _ = sc._check_requirement(req(covers=["app.cart.Cart.add"]), CANON)
        self.assertEqual(checked["covers"], ["Cart.add"])

    def test_covers_is_capped_at_eight_and_deduplicated(self):
        many = ["Cart.add"] * 3 + ["checkout"] * 10
        checked, _ = sc._check_requirement(req(covers=many), CANON)
        self.assertLessEqual(len(checked["covers"]), 8)
        self.assertEqual(len(checked["covers"]), len(set(checked["covers"])))

    def test_rejects_unknown_type_and_status_with_a_reason(self):
        self.assertIsNone(sc._check_requirement(req(type="vibe"), CANON)[0])
        self.assertIsNone(sc._check_requirement(req(status="probably"), CANON)[0])
        self.assertIsNone(sc._check_requirement(req(text="  "), CANON)[0])
        self.assertIsNone(sc._check_requirement("not a dict", CANON)[0])


class ValidateRequirementsTests(unittest.TestCase):
    def test_filters_out_only_the_invalid_ones(self):
        raw = [req(), req(type="nope"), req(text="second real one", covers=["checkout"])]
        out = sc.validate_requirements(CONTEXT, raw)
        self.assertEqual(len(out), 2)


class ComputeTestedStatusTests(unittest.TestCase):
    def test_a_requirement_covered_by_a_test_case_is_marked_tested(self):
        requirements = [{"text": "a", "covers": ["Cart.add"]}]
        cases = [{"title": "Add to cart happy path", "covers": ["Cart.add"]}]
        out = sc.compute_tested_status(requirements, cases)
        self.assertTrue(out[0]["tested"])
        self.assertEqual(out[0]["tested_by"], ["Add to cart happy path"])

    def test_a_requirement_with_no_overlapping_test_case_is_not_tested(self):
        requirements = [{"text": "a", "covers": ["Cart.remove"]}]
        cases = [{"title": "Unrelated case", "covers": ["checkout"]}]
        out = sc.compute_tested_status(requirements, cases)
        self.assertFalse(out[0]["tested"])
        self.assertEqual(out[0]["tested_by"], [])


class LinkRequirementsToCasesTests(unittest.TestCase):
    """The reverse direction of ComputeTestedStatusTests -- from a test
    case's point of view, which story requirements (across every document)
    does it relate to."""

    def test_a_case_links_to_every_requirement_sharing_a_citation(self):
        cases = [{"id": "c1", "covers": ["Cart.add"]}]
        requirements = [
            {"id": "r1", "doc_id": "d1", "text": "Users can add items", "status": "supported", "covers": ["Cart.add"]},
            {"id": "r2", "doc_id": "d2", "text": "Unrelated requirement", "status": "unknown", "covers": ["checkout"]},
        ]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(len(out[0]["related_requirements"]), 1)
        self.assertEqual(out[0]["related_requirements"][0]["text"], "Users can add items")

    def test_a_requirement_matched_by_two_different_citations_is_not_duplicated(self):
        cases = [{"id": "c1", "covers": ["Cart.add", "Cart.remove"]}]
        requirements = [{"id": "r1", "doc_id": "d1", "text": "Cart management", "status": "supported",
                          "covers": ["Cart.add", "Cart.remove"]}]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(len(out[0]["related_requirements"]), 1)

    def test_a_case_with_no_matching_requirement_gets_an_empty_list_not_an_error(self):
        cases = [{"id": "c1", "covers": ["Something.unrelated"]}]
        requirements = [{"id": "r1", "doc_id": "d1", "text": "x", "status": "unknown", "covers": ["checkout"]}]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(out[0]["related_requirements"], [])

    def test_original_case_fields_are_preserved(self):
        cases = [{"id": "c1", "title": "Add to cart", "covers": []}]
        out = sc.link_requirements_to_cases(cases, [])
        self.assertEqual(out[0]["title"], "Add to cart")

    def test_source_doc_id_comes_from_the_explicit_source_requirement_id_not_the_heuristic(self):
        # A case generated FROM r1 (source_requirement_id) that ALSO happens to
        # share a citation with unrelated r2 -- source_doc_id must point to r1's
        # document, not get confused by the heuristic overlap with r2.
        cases = [{"id": "c1", "source_requirement_id": "r1", "covers": ["checkout"]}]
        requirements = [
            {"id": "r1", "doc_id": "doc-origin", "text": "the real origin", "status": "supported", "covers": ["checkout"]},
            {"id": "r2", "doc_id": "doc-other", "text": "unrelated but shares a citation", "status": "unknown", "covers": ["checkout"]},
        ]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(out[0]["source_doc_id"], "doc-origin")

    def test_source_doc_id_is_none_for_a_code_generated_case(self):
        cases = [{"id": "c1", "covers": []}]  # no source_requirement_id at all
        out = sc.link_requirements_to_cases(cases, [])
        self.assertIsNone(out[0]["source_doc_id"])

    def test_a_pure_code_driven_case_adopts_the_worst_of_its_matched_requirements_as_its_own_verdict(self):
        # Module-level generation is grounded in real code now, so there's no need for a
        # second LLM call to verify it against the story -- Story Coverage already judged
        # the matching requirement(s) against this same code; a code-driven case just
        # adopts the worst of those verdicts as its own, for free, at read time.
        cases = [{"id": "c1", "covers": ["Inventory.reserve"]}]  # no source_requirement_id/source_doc_id/source_journey_id
        requirements = [
            {"id": "r1", "doc_id": "d1", "text": "Reserving stock never oversells", "status": "contradicted",
             "covers": ["Inventory.reserve"], "rationale": "reserve() allows the count to go negative"},
            {"id": "r2", "doc_id": "d2", "text": "Reservations are logged", "status": "supported",
             "covers": ["Inventory.reserve"], "rationale": "a log entry is written"},
        ]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(out[0]["status"], "contradicted")
        self.assertEqual(out[0]["rationale"], "reserve() allows the count to go negative")

    def test_a_code_driven_case_matching_nothing_gets_no_status(self):
        cases = [{"id": "c1", "covers": ["Something.unrelated"]}]
        requirements = [{"id": "r1", "doc_id": "d1", "text": "x", "status": "contradicted", "covers": ["checkout"]}]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertNotIn("status", out[0])

    def test_a_case_with_its_own_stored_status_is_never_overridden_even_if_it_shares_a_citation(self):
        cases = [{"id": "c1", "status": "supported", "rationale": "already verified", "covers": ["Inventory.reserve"]}]
        requirements = [{"id": "r1", "doc_id": "d1", "text": "x", "status": "contradicted", "covers": ["Inventory.reserve"]}]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertEqual(out[0]["status"], "supported")
        self.assertEqual(out[0]["rationale"], "already verified")

    def test_a_story_driven_case_identified_by_source_requirement_id_is_never_given_a_derived_status(self):
        # Has source_requirement_id -- not "pure code-driven" -- even with no stored status of
        # its own and a citation overlap, it must stay untouched (its own Verify pass owns this).
        cases = [{"id": "c1", "source_requirement_id": "r1", "covers": ["Inventory.reserve"]}]
        requirements = [{"id": "r1", "doc_id": "d1", "text": "x", "status": "contradicted", "covers": ["Inventory.reserve"]}]
        out = sc.link_requirements_to_cases(cases, requirements)
        self.assertNotIn("status", out[0])


class GenerateStoryRequirementsTests(unittest.TestCase):
    """Mocks openai.OpenAI to exercise generate_story_requirements()'s two-call
    shape: one decompose call (text+type only), then one retrieval-narrowed
    judgment call per decomposed criterion -- prompt shape, response parsing,
    and citation validation all wired together, without a real API key or
    network call. The embedding index is pre-seeded on disk so retrieval's
    OWN embeddings call (kg/embeddings.py, tested separately in
    tests/test_embeddings.py) never needs to be exercised here."""

    def _run(self, requirements_payload, context=CONTEXT, doc=DOC, raw_decompose=None):
        decompose_resp = mock.Mock()
        decompose_resp.choices = [mock.Mock(message=mock.Mock(content=json.dumps(
            raw_decompose if raw_decompose is not None else
            {"requirements": [{"text": r["text"], "type": r["type"]} for r in requirements_payload if isinstance(r, dict) and r.get("text")]}
        )))]
        judge_resps_by_text: dict[str, mock.Mock] = {}
        for r in requirements_payload:
            if not (isinstance(r, dict) and r.get("text")):
                continue
            jr = mock.Mock()
            jr.choices = [mock.Mock(message=mock.Mock(content=json.dumps(
                {"status": r["status"], "covers": r.get("covers", []), "rationale": r.get("rationale")}
            )))]
            judge_resps_by_text[r["text"]] = jr
        judge_resps = list(judge_resps_by_text.values())

        decompose_queue = [decompose_resp]
        if not requirements_payload and len(doc.get("content", "").strip()) > sc._MIN_CONTENT_FOR_RETRY:
            # Zero decomposed atoms on a non-trivial document triggers
            # generate_story_requirements's own completeness retry -- queue
            # the same (also-empty) response again so that second call
            # doesn't starve the mock.
            decompose_queue.append(decompose_resp)
        decompose_lock = threading.Lock()

        def create_side_effect(*args, **kwargs):
            # The per-atom judge loop runs concurrently (a bounded thread
            # pool -- see generate_story_requirements), so several threads
            # can call this mock at once. A plain positional side_effect
            # list would hand out responses in whatever order the threads
            # happen to race the shared iterator, not atom order -- flaky.
            # Matching on the criterion text embedded in the judge prompt
            # (see _judge_prompt) makes the response correct regardless of
            # scheduling; the decompose call(s) are told apart by a locked
            # queue since they carry no atom text to match on.
            with decompose_lock:
                if decompose_queue:
                    return decompose_queue.pop(0)
            prompt = kwargs["messages"][0]["content"]
            for text, resp in judge_resps_by_text.items():
                if text in prompt:
                    return resp
            raise AssertionError(f"no mocked judge response matches prompt: {prompt[:200]!r}")

        client = mock.Mock()
        client.chat.completions.create.side_effect = create_side_effect
        client.embeddings.create.return_value = mock.Mock(data=[mock.Mock(embedding=[1.0]) for _ in judge_resps])

        index_path = Path(tempfile.mkdtemp()) / "embeddings.json"
        embeddings.save_index([{"module": "cart", "text": "x", "embedding": [1.0]}], index_path)
        g = nx.MultiDiGraph()

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch("openai.OpenAI", return_value=client), \
             mock.patch("server.llm_story_coverage.gather_module_sources", return_value={}):
            return sc.generate_story_requirements(doc, context, g, Path("/tmp"), index_path), client

    def test_a_real_citation_survives_and_a_fake_one_is_demoted(self):
        result, client = self._run([
            {"text": "Users can add items to the cart", "type": "positive", "status": "supported",
             "covers": ["Cart.add"], "rationale": "Cart.add appends an item"},
            {"text": "Users can apply a discount code", "type": "positive", "status": "supported",
             "covers": ["DiscountEngine.apply"], "rationale": "invented"},
        ])
        self.assertEqual(client.chat.completions.create.call_count, 3)  # 1 decompose + 2 judge calls
        by_text = {r["text"]: r for r in result.requirements}
        self.assertEqual(by_text["Users can add items to the cart"]["status"], "supported")
        self.assertEqual(by_text["Users can apply a discount code"]["status"], "unknown")
        self.assertEqual(by_text["Users can apply a discount code"]["covers"], [])
        self.assertEqual(result.dropped, [])

    def test_the_decompose_prompt_includes_the_document(self):
        _, client = self._run([])
        prompt = client.chat.completions.create.call_args_list[0].kwargs["messages"][0]["content"]
        self.assertIn(DOC["name"], prompt)
        self.assertIn(DOC["content"], prompt)

    def test_the_judge_prompt_includes_the_criterion_and_retrieved_source(self):
        with mock.patch("server.llm_story_coverage.gather_module_sources", return_value={"Cart.add": "def add(self, item): ..."}):
            _, client = self._run([
                {"text": "Users can add items to the cart", "type": "positive", "status": "supported", "covers": ["Cart.add"]},
            ])
        judge_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("Users can add items to the cart", judge_prompt)
        self.assertIn("REAL SOURCE CODE", judge_prompt)

    def test_an_invalid_decomposed_item_is_dropped_not_judged(self):
        result, client = self._run(
            [{"text": "ok one", "type": "positive", "status": "unknown", "covers": []}],
            raw_decompose={"requirements": [
                {"text": "ok one", "type": "positive"},
                {"text": "bad type", "type": "vibes"},
                "junk",
            ]},
        )
        # only the one valid atom gets judged -- the decompose call plus exactly one judge call
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_empty_list_is_a_valid_answer(self):
        result, client = self._run([])
        self.assertEqual(result.requirements, [])
        self.assertEqual(result.dropped, [])
        # decompose, then one completeness retry (DOC is non-trivial content
        # that still came back empty) -- no judge calls either way
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                sc.generate_story_requirements(DOC, CONTEXT, nx.MultiDiGraph(), Path("/tmp"))
        self.assertIn("isn't configured", str(ctx.exception))

    def test_malformed_json_from_the_model_is_a_clear_runtime_error(self):
        r = mock.Mock()
        r.choices = [mock.Mock(message=mock.Mock(content="not json"))]
        client = mock.Mock()
        client.chat.completions.create.return_value = r
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                sc.generate_story_requirements(DOC, CONTEXT, nx.MultiDiGraph(), Path("/tmp"))
        self.assertIn("valid JSON", str(ctx.exception))

    def test_an_api_failure_during_decompose_is_surfaced_not_swallowed(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                sc.generate_story_requirements(DOC, CONTEXT, nx.MultiDiGraph(), Path("/tmp"))
        self.assertIn("rate limited", str(ctx.exception))

    def test_a_requirements_field_that_isnt_a_list_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run([], raw_decompose={"requirements": "not a list"})
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_model_override_env_var_is_respected(self):
        with mock.patch.dict(os.environ, {"OPENAI_STORY_COVERAGE_MODEL": "gpt-4o"}):
            _, client = self._run([])
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "gpt-4o")


class StoryRequirementsStorageTests(unittest.TestCase):
    """DB-level: the one contract most worth a direct test -- a replace pass
    for this document never touches a human-confirmed row, for this
    document or any other."""

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
        self.doc = db.create_document(self.repo["id"], {"name": "story", "content": "x"})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_replace_only_touches_this_documents_own_document_sourced_rows(self):
        other_doc = db.create_document(self.repo["id"], {"name": "other story", "content": "y"})
        db.replace_story_requirements(self.repo["id"], self.doc["id"], None, [
            {"text": "first pass", "type": "positive", "status": "unknown", "covers": []},
        ])
        db.replace_story_requirements(self.repo["id"], other_doc["id"], None, [
            {"text": "other doc's requirement", "type": "positive", "status": "unknown", "covers": []},
        ])
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO story_requirements (id, repo_id, doc_id, run_id, text, type, status, rationale, "
                "covers, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.repo["id"], self.doc["id"], None, "confirmed one", "positive",
                 "supported", "r", json.dumps([]), "human_confirmed", db.now()),
            )

        db.replace_story_requirements(self.repo["id"], self.doc["id"], None, [
            {"text": "second pass", "type": "negative", "status": "contradicted", "covers": ["Cart.add"]},
        ])

        rows = db.list_story_requirements(self.repo["id"], self.doc["id"])
        texts_and_sources = sorted((r["text"], r["source"]) for r in rows)
        self.assertEqual(texts_and_sources, [("confirmed one", "human_confirmed"), ("second pass", "document")])
        # the other document's own requirement was never touched by either replace call above
        other_rows = db.list_story_requirements(self.repo["id"], other_doc["id"])
        self.assertEqual([r["text"] for r in other_rows], ["other doc's requirement"])

    def test_deleting_the_document_cascades(self):
        db.replace_story_requirements(self.repo["id"], self.doc["id"], None, [
            {"text": "x", "type": "positive", "status": "unknown", "covers": []},
        ])
        db.delete_document(self.doc["id"])
        self.assertEqual(db.list_story_requirements(self.repo["id"], self.doc["id"]), [])


class StoryCoverageApiTests(unittest.TestCase):
    """No TESTATLAS_PASSWORD and no OPENAI_API_KEY here -- exercises only the
    free paths (context + manual submit) and confirms the paid path gates
    cleanly, same shape as gap analysis's equivalent endpoints."""

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
            "    def remove(self, item):\n        pass\n\n"
            "def checkout(cart):\n"
            "    if not cart:\n        raise ValueError('empty cart')\n"
            "    return True\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        r = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
            "name": "Checkout story",
            "content": "Users can add items to the cart. Users cannot checkout with an empty cart.",
        })
        self.doc_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_config_reports_unavailable_with_no_key(self):
        r = self.client.get("/api/story-coverage/config")
        self.assertEqual(r.json(), {"automatic_available": False})

    def test_context_endpoint_is_free_and_scoped_to_one_document(self):
        r = self.client.get(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-coverage-context")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["documents"]), 1)
        self.assertEqual(body["modules"][0]["module"], "cart")

    def test_automatic_run_without_a_key_is_rejected_not_silently_skipped(self):
        r = self.client.post(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-coverage/run")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't configured", r.json()["detail"])

    def test_manual_submit_demotes_a_fabricated_citation_and_keeps_a_real_one(self):
        payload = {"requirements": [
            {"text": "Users can add items to the cart", "type": "positive", "status": "supported",
             "covers": ["Cart.add"], "rationale": "r"},
            {"text": "fake thing", "type": "positive", "status": "supported",
             "covers": ["NotReal.fake"], "rationale": "r"},
        ]}
        r = self.client.post(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements", json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stored"], 2)

        r = self.client.get(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements")
        by_text = {row["text"]: row for row in r.json()}
        self.assertEqual(by_text["Users can add items to the cart"]["status"], "supported")
        self.assertEqual(by_text["fake thing"]["status"], "unknown")
        self.assertEqual(by_text["fake thing"]["covers"], [])

    def test_an_invalid_status_in_the_manual_submission_is_a_400_not_silently_dropped(self):
        payload = {"requirements": [{"text": "x", "type": "positive", "status": "vibes", "covers": []}]}
        r = self.client.post(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements", json=payload)
        self.assertEqual(r.status_code, 400)

    def test_tested_status_reflects_an_existing_test_case(self):
        self.client.post(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements", json={
            "requirements": [{"text": "a", "type": "positive", "status": "supported", "covers": ["Cart.add"]}],
        })
        db.replace_test_cases(self.repo_id, "cart", None, [{
            "title": "Add to cart happy path", "category": "happy_path",
            "steps": [{"action": "x", "expected": "y"}], "expected_result": "y", "covers": ["Cart.add"],
        }])
        r = self.client.get(f"/api/repos/{self.repo_id}/documents/{self.doc_id}/story-requirements")
        row = next(row for row in r.json() if row["text"] == "a")
        self.assertTrue(row["tested"])
        self.assertIn("Add to cart happy path", row["tested_by"])


if __name__ == "__main__":
    unittest.main()
