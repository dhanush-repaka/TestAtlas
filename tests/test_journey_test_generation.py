"""Tests for generating ONE end-to-end test case from a journey's already-
judged member requirements (server/llm_journey_test_generation.py). The core
safety property: covers/status/rationale are always DERIVED from the
requirements' own judgments, positionally matched to steps -- never trusted
from whatever the mocked model response itself claims, and the overall
status is the worst among the members (never collapsed to a fake single
verdict that would hide one bad member).
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from server import llm_journey_test_generation as ljtg

REQUIREMENTS = [
    {"id": "r1", "text": "User logs in", "type": "positive", "status": "supported",
     "covers": ["login"], "rationale": "login() exists and works"},
    {"id": "r2", "text": "Cart total is charged", "type": "positive", "status": "contradicted",
     "covers": ["charge_card"], "rationale": "charge_card never actually charges"},
]


def mock_model_response(payload):
    r = mock.Mock()
    r.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
    client = mock.Mock()
    client.chat.completions.create.return_value = r
    return client


class GenerateTestCaseFromJourneyTests(unittest.TestCase):
    def _run(self, steps_payload, requirements=None, sources=None):
        requirements = requirements if requirements is not None else REQUIREMENTS
        client = mock_model_response({"test_case": {
            "title": "Checkout flow", "category": "happy_path", "priority": 1,
            "steps": steps_payload,
        }})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return ljtg.generate_test_case_from_journey("Checkout", requirements, sources or {}), client

    def test_overall_status_is_the_worst_among_members(self):
        case, _ = self._run([
            {"action": "Log in", "expected": "Logged in", "status": "supported"},  # model's own guess, ignored
            {"action": "Charge card", "expected": "Card is charged"},
        ])
        self.assertEqual(case["status"], "contradicted")  # worst member wins, not the model's claim

    def test_covers_is_the_union_of_every_members_citations(self):
        case, _ = self._run([{"action": "a", "expected": "b"}, {"action": "c", "expected": "d"}])
        self.assertEqual(case["covers"], ["charge_card", "login"])

    def test_steps_are_tagged_positionally_with_their_backing_requirement(self):
        case, _ = self._run([
            {"action": "Log in", "expected": "Logged in"},
            {"action": "Charge card", "expected": "Card is charged"},
        ])
        self.assertEqual(case["steps"][0]["status"], "supported")
        self.assertEqual(case["steps"][0]["requirement_text"], "User logs in")
        self.assertEqual(case["steps"][1]["status"], "contradicted")
        self.assertEqual(case["steps"][1]["requirement_text"], "Cart total is charged")

    def test_an_extra_step_past_the_given_requirements_is_left_untagged(self):
        case, _ = self._run([
            {"action": "a", "expected": "b"},
            {"action": "c", "expected": "d"},
            {"action": "extra step the model added", "expected": "e"},
        ])
        self.assertIsNone(case["steps"][2]["status"])
        self.assertIsNone(case["steps"][2]["requirement_text"])

    def test_all_supported_members_yield_supported_overall(self):
        case, _ = self._run(
            [{"action": "a", "expected": "b"}],
            requirements=[{"id": "r1", "text": "x", "type": "positive", "status": "supported", "covers": ["login"]}],
        )
        self.assertEqual(case["status"], "supported")

    def test_no_requirements_is_rejected_before_any_network_call(self):
        client = mock_model_response({"test_case": {}})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljtg.generate_test_case_from_journey("Checkout", [], {})
        self.assertIn("no judged requirements", str(ctx.exception))
        client.chat.completions.create.assert_not_called()

    def test_missing_title_is_rejected_with_a_clear_runtime_error(self):
        client = mock_model_response({"test_case": {
            "category": "happy_path", "priority": 1, "steps": [{"action": "a", "expected": "b"}],
        }})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljtg.generate_test_case_from_journey("Checkout", REQUIREMENTS, {})
        self.assertIn("rejected", str(ctx.exception))

    def test_missing_steps_is_rejected(self):
        client = mock_model_response({"test_case": {"title": "x", "category": "happy_path", "priority": 1, "steps": []}})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError):
                ljtg.generate_test_case_from_journey("Checkout", REQUIREMENTS, {})

    def test_test_case_not_a_dict_is_rejected_cleanly(self):
        client = mock_model_response({"test_case": "not an object"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljtg.generate_test_case_from_journey("Checkout", REQUIREMENTS, {})
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                ljtg.generate_test_case_from_journey("Checkout", REQUIREMENTS, {})
        self.assertIn("isn't configured", str(ctx.exception))

    def test_an_api_failure_is_surfaced_not_swallowed(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljtg.generate_test_case_from_journey("Checkout", REQUIREMENTS, {})
        self.assertIn("rate limited", str(ctx.exception))

    def test_real_source_is_included_in_the_prompt_when_given(self):
        _, client = self._run(
            [{"action": "a", "expected": "b"}, {"action": "c", "expected": "d"}],
            sources={"login": "def login(user):\n    return True"},
        )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("REAL SOURCE CODE", prompt)
        self.assertIn("def login(user):", prompt)


if __name__ == "__main__":
    unittest.main()
