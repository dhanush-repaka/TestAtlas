"""Tests for deriving candidate requirements from a journey's real code
(server/llm_journey_requirements.py) -- the same citation-validated,
never-a-bare-guess discipline every other LLM feature here uses, plus the one
new case this feature adds: a real pattern with genuine ambiguity becomes
status="needs_human_input" with a question, instead of a guessed verdict.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from server import llm_journey_requirements as ljr

SOURCES = {"charge_card": "def charge_card(cart):\n    return gateway.charge(cart.total)"}


def mock_model_response(payload):
    r = mock.Mock()
    r.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
    client = mock.Mock()
    client.chat.completions.create.return_value = r
    return client


class GenerateJourneyRequirementsTests(unittest.TestCase):
    def _run(self, requirements_payload, sources=None):
        client = mock_model_response({"requirements": requirements_payload})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return ljr.generate_journey_requirements("Checkout", sources if sources is not None else SOURCES), client

    def test_a_cited_supported_claim_is_kept(self):
        out, _ = self._run([
            {"text": "Payment is charged via the gateway", "type": "positive", "status": "supported",
             "covers": ["charge_card"], "rationale": "calls gateway.charge"},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "supported")

    def test_an_uncited_supported_claim_is_demoted_to_unknown(self):
        out, _ = self._run([
            {"text": "Something is true", "type": "positive", "status": "supported", "covers": []},
        ])
        self.assertEqual(out[0]["status"], "unknown")

    def test_needs_human_input_with_a_question_and_citation_is_kept(self):
        out, _ = self._run([
            {"text": "Cancellation is allowed after dispatch", "type": "negative", "status": "needs_human_input",
             "covers": ["charge_card"], "question": "Should cancellation be allowed after dispatch?",
             "rationale": "found no guard against it"},
        ])
        self.assertEqual(out[0]["status"], "needs_human_input")
        self.assertEqual(out[0]["question"], "Should cancellation be allowed after dispatch?")

    def test_needs_human_input_without_a_question_is_demoted_to_unknown(self):
        out, _ = self._run([
            {"text": "x", "type": "positive", "status": "needs_human_input", "covers": ["charge_card"]},
        ])
        self.assertEqual(out[0]["status"], "unknown")
        self.assertIsNone(out[0]["question"])

    def test_needs_human_input_without_a_citation_is_demoted_to_unknown(self):
        out, _ = self._run([
            {"text": "x", "type": "positive", "status": "needs_human_input", "covers": [],
             "question": "is this ok?"},
        ])
        self.assertEqual(out[0]["status"], "unknown")

    def test_a_fabricated_citation_is_dropped_not_trusted(self):
        out, _ = self._run([
            {"text": "x", "type": "positive", "status": "supported", "covers": ["TotallyMadeUp"]},
        ])
        self.assertEqual(out[0]["status"], "unknown")
        self.assertEqual(out[0]["covers"], [])

    def test_no_sources_returns_empty_without_calling_the_model(self):
        client = mock_model_response({"requirements": []})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            out = ljr.generate_journey_requirements("Checkout", {})
        self.assertEqual(out, [])
        client.chat.completions.create.assert_not_called()

    def test_missing_api_key_raises_before_ever_touching_the_network(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                ljr.generate_journey_requirements("Checkout", SOURCES)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_an_api_failure_is_surfaced_not_swallowed(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljr.generate_journey_requirements("Checkout", SOURCES)
        self.assertIn("rate limited", str(ctx.exception))

    def test_malformed_shape_is_rejected_cleanly(self):
        client = mock_model_response({"requirements": "not a list"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                ljr.generate_journey_requirements("Checkout", SOURCES)
        self.assertIn("unexpected shape", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
