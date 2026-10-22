"""Tests for Test Case Verification (server/llm_test_verification.py), the
mechanism behind the document-scoped Verify pass (server/app.py's
api_verify_test_cases_for_document, tested in
tests/test_document_test_verification.py). GenerateTestCaseVerificationsTests
mocks the OpenAI client (same style as tests/test_story_coverage.py's
GenerateStoryRequirementsTests) to exercise generate_test_case_verifications()
end-to-end. Every other test class checks what WE control directly:
validation, the never-cite-a-fake-name guarantee, and id-matching (never
apply a judgment to the wrong row).

(There is no module-scoped verify anymore -- a module-level case is now
grounded in real source at generation time, so its status/rationale are
derived for free from Story Coverage's own judgments instead of a second,
redundant check against the same code; see
server/llm_story_coverage.py's link_requirements_to_cases.)
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from server import llm_test_verification as tv

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
CANON = tv.known_code_names(CONTEXT)


def case(**over):
    base = {"id": "case-1", "title": "Add to cart happy path", "category": "happy_path",
            "steps": [{"action": "Add an item", "expected": "It appears in the cart"}],
            "expected_result": "Item in cart", "covers": ["Cart.add"]}
    return {**base, **over}


class CheckVerificationTests(unittest.TestCase):
    def test_a_real_citation_keeps_its_verdict(self):
        item = {"id": "case-1", "status": "supported", "covers": ["Cart.add"], "rationale": "found it"}
        checked, why = tv._check_verification(item, {"case-1"}, CANON)
        self.assertIsNone(why)
        self.assertEqual(checked["status"], "supported")
        self.assertEqual(checked["covers"], ["Cart.add"])

    def test_a_fake_citation_demotes_supported_to_unknown(self):
        item = {"id": "case-1", "status": "supported", "covers": ["NotReal.fake"], "rationale": "r"}
        checked, why = tv._check_verification(item, {"case-1"}, CANON)
        self.assertIsNone(why)
        self.assertEqual(checked["status"], "unknown")
        self.assertEqual(checked["covers"], [])

    def test_contradicted_is_demoted_the_same_way(self):
        item = {"id": "case-1", "status": "contradicted", "covers": ["invented"], "rationale": "r"}
        checked, _ = tv._check_verification(item, {"case-1"}, CANON)
        self.assertEqual(checked["status"], "unknown")

    def test_an_id_not_in_the_input_set_is_dropped_never_applied(self):
        item = {"id": "case-999", "status": "supported", "covers": ["Cart.add"], "rationale": "r"}
        checked, why = tv._check_verification(item, {"case-1"}, CANON)
        self.assertIsNone(checked)
        self.assertIn("unknown or missing id", why)

    def test_a_missing_id_is_rejected(self):
        item = {"status": "supported", "covers": [], "rationale": "r"}
        checked, why = tv._check_verification(item, {"case-1"}, CANON)
        self.assertIsNone(checked)

    def test_rejects_unknown_status(self):
        item = {"id": "case-1", "status": "probably", "covers": [], "rationale": "r"}
        checked, why = tv._check_verification(item, {"case-1"}, CANON)
        self.assertIsNone(checked)
        self.assertIn("unknown status", why)

    def test_not_a_dict_is_rejected(self):
        checked, _ = tv._check_verification("junk", {"case-1"}, CANON)
        self.assertIsNone(checked)


class GenerateTestCaseVerificationsTests(unittest.TestCase):
    """Mocks openai.OpenAI -- same style as test_story_coverage.py's
    GenerateStoryRequirementsTests -- to exercise generate_test_case_verifications()
    itself, the function the rest of this file deliberately doesn't reach."""

    def _run(self, payload, cases=None, context=CONTEXT, sources=None):
        cases = cases if cases is not None else [case()]
        r = mock.Mock()
        r.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
        client = mock.Mock()
        client.chat.completions.create.return_value = r
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return tv.generate_test_case_verifications(cases, context, sources), client

    def test_a_real_citation_survives_and_a_fake_one_is_demoted(self):
        result, client = self._run({"verifications": [
            {"id": "case-1", "status": "supported", "covers": ["Cart.add"], "rationale": "Cart.add appends an item"},
            {"id": "case-2", "status": "supported", "covers": ["NotReal.fake"], "rationale": "invented"},
        ]}, cases=[case(id="case-1"), case(id="case-2", title="Apply a discount code", covers=["DiscountEngine.apply"])])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        by_id = {r["id"]: r for r in result.results}
        self.assertEqual(by_id["case-1"]["status"], "supported")
        self.assertEqual(by_id["case-2"]["status"], "unknown")
        self.assertEqual(by_id["case-2"]["covers"], [])
        self.assertEqual(result.dropped, [])

    def test_the_prompt_includes_the_cases_and_the_real_code_names(self):
        _, client = self._run({"verifications": []}, cases=[case()])
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        for needle in ("case-1", "Add to cart happy path", "Cart", "checkout", "JUDGE" if "JUDGE" in prompt else "judge"):
            self.assertIn(needle, prompt)

    def test_real_source_is_included_in_the_prompt_when_given(self):
        _, client = self._run({"verifications": []}, sources={"Cart.add": "def add(self, item):\n    self.items.append(item)"})
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("REAL SOURCE CODE", prompt)
        self.assertIn("self.items.append(item)", prompt)

    def test_no_source_block_at_all_when_nothing_resolved(self):
        _, client = self._run({"verifications": []}, sources={})
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("REAL SOURCE CODE", prompt)

    def test_a_case_the_model_never_addresses_is_simply_absent_not_forced_to_unknown(self):
        result, _ = self._run({"verifications": []}, cases=[case()])
        self.assertEqual(result.results, [])  # not force-filled with an "unknown" entry

    def test_an_invalid_item_is_dropped_and_reported_not_silently_lost(self):
        result, _ = self._run({"verifications": [
            {"id": "case-1", "status": "unknown", "covers": []},
            {"id": "case-1", "status": "vibes", "covers": []},
            "junk",
        ]})
        self.assertEqual(len(result.results), 1)
        self.assertEqual(len(result.dropped), 2)

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                tv.generate_test_case_verifications([case()], CONTEXT)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_malformed_json_from_the_model_is_a_clear_runtime_error(self):
        r = mock.Mock()
        r.choices = [mock.Mock(message=mock.Mock(content="not json"))]
        client = mock.Mock()
        client.chat.completions.create.return_value = r
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                tv.generate_test_case_verifications([case()], CONTEXT)
        self.assertIn("valid JSON", str(ctx.exception))

    def test_an_api_failure_is_surfaced_not_swallowed(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                tv.generate_test_case_verifications([case()], CONTEXT)
        self.assertIn("rate limited", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
