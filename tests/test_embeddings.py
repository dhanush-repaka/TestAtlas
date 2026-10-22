"""Tests for kg/embeddings.py (a per-run module retrieval index) and for the
new two-call shape it enables in server/llm_story_coverage.py's
generate_story_requirements: decompose once, then retrieve + judge each
criterion against a narrowed, real-source-backed shortlist instead of a flat
whole-codebase dump.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import networkx as nx

from kg import embeddings as kemb
from server import llm_story_coverage as lsc

DOC = {"name": "payments", "content": "Users can pay for their order by card."}

CONTEXT = {
    "modules": [
        {"module": "checkout", "description": "handles payment", "files": [
            {"file": "checkout.payments", "functions": ["pay_now"], "classes": [], "ui_text": []},
        ]},
        {"module": "search", "description": "handles search", "files": [
            {"file": "search.query", "functions": ["run_search"], "classes": [], "ui_text": []},
        ]},
    ],
}


class CosineAndRetrievalTests(unittest.TestCase):
    def test_identical_vectors_score_highest(self):
        index = [
            {"module": "checkout", "embedding": [1.0, 0.0]},
            {"module": "search", "embedding": [0.0, 1.0]},
        ]
        self.assertEqual(kemb.retrieve_candidate_modules(index, [1.0, 0.0], k=1), ["checkout"])
        self.assertEqual(kemb.retrieve_candidate_modules(index, [0.0, 1.0], k=1), ["search"])

    def test_closer_vector_ranks_first(self):
        index = [
            {"module": "checkout", "embedding": [1.0, 0.0]},
            {"module": "search", "embedding": [0.0, 1.0]},
        ]
        ranked = kemb.retrieve_candidate_modules(index, [0.9, 0.1], k=2)
        self.assertEqual(ranked, ["checkout", "search"])

    def test_zero_vector_never_errors(self):
        index = [{"module": "a", "embedding": [0.0, 0.0]}]
        self.assertEqual(kemb.retrieve_candidate_modules(index, [1.0, 0.0], k=1), ["a"])


class SaveLoadIndexTests(unittest.TestCase):
    def test_round_trip(self):
        tmp = Path(tempfile.mkdtemp()) / "embeddings.json"
        index = [{"module": "checkout", "text": "x", "embedding": [1.0, 2.0]}]
        kemb.save_index(index, tmp)
        self.assertEqual(kemb.load_index(tmp), index)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(kemb.load_index(Path("/does/not/exist.json")), [])


class BuildModuleIndexAndEmbedTextsTests(unittest.TestCase):
    def _mock_client(self, n_vectors):
        resp = mock.Mock()
        resp.data = [mock.Mock(embedding=[float(i), 0.0]) for i in range(n_vectors)]
        client = mock.Mock()
        client.embeddings.create.return_value = resp
        return client

    def test_build_module_index_embeds_every_module_once(self):
        client = self._mock_client(2)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            index = kemb.build_module_index(CONTEXT)
        self.assertEqual([e["module"] for e in index], ["checkout", "search"])
        client.embeddings.create.assert_called_once()

    def test_missing_api_key_raises_before_any_network_call(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                kemb.build_module_index(CONTEXT)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_embed_texts_returns_one_vector_per_input(self):
        client = self._mock_client(3)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            out = kemb.embed_texts(["a", "b", "c"])
        self.assertEqual(len(out), 3)


def mock_chat_response(payload):
    return mock.Mock(choices=[mock.Mock(message=mock.Mock(content=json.dumps(payload)))])


class GenerateStoryRequirementsRetrievalShapeTests(unittest.TestCase):
    """Pre-seeds the embedding index (so build_module_index's own API call is
    never exercised here -- that's covered above) and asserts the new
    decompose-then-per-requirement-judge shape: one decompose call, one query
    embedding call, one judge call per decomposed criterion, each judge call
    scoped to retrieval's narrowed module shortlist."""

    def setUp(self):
        self.index_path = Path(tempfile.mkdtemp()) / "embeddings.json"
        kemb.save_index([
            {"module": "checkout", "text": "handles payment", "embedding": [1.0, 0.0]},
            {"module": "search", "text": "handles search", "embedding": [0.0, 1.0]},
        ], self.index_path)
        self.g = nx.MultiDiGraph()  # empty -- gather_module_sources just returns {} on it, which is fine here

    def test_decompose_then_one_judge_call_per_criterion_narrowed_by_retrieval(self):
        chat_responses = [
            mock_chat_response({"requirements": [{"text": "Users can pay by card", "type": "positive"}]}),
            mock_chat_response({"status": "supported", "covers": ["pay_now"], "rationale": "pay_now exists"}),
        ]
        client = mock.Mock()
        client.chat.completions.create.side_effect = chat_responses
        client.embeddings.create.return_value = mock.Mock(data=[mock.Mock(embedding=[0.9, 0.1])])  # closer to "checkout"

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch("openai.OpenAI", return_value=client), \
             mock.patch("server.llm_story_coverage.gather_module_sources") as gather_mock:
            gather_mock.return_value = {}
            result = lsc.generate_story_requirements(DOC, CONTEXT, self.g, Path("/tmp"), self.index_path)

        self.assertEqual(client.chat.completions.create.call_count, 2)  # decompose + 1 judge, not 1 combined call
        self.assertEqual(client.embeddings.create.call_count, 1)  # the index was pre-seeded, never rebuilt
        narrowed_modules = gather_mock.call_args.args[2]
        self.assertEqual(narrowed_modules[0], "checkout")  # retrieval ranked the closer module first

        self.assertEqual(len(result.requirements), 1)
        self.assertEqual(result.requirements[0]["status"], "supported")
        self.assertEqual(result.requirements[0]["covers"], ["pay_now"])

    def test_a_fabricated_citation_in_the_judge_response_is_dropped(self):
        chat_responses = [
            mock_chat_response({"requirements": [{"text": "Users can pay by card", "type": "positive"}]}),
            mock_chat_response({"status": "supported", "covers": ["NotARealFunction"], "rationale": "x"}),
        ]
        client = mock.Mock()
        client.chat.completions.create.side_effect = chat_responses
        client.embeddings.create.return_value = mock.Mock(data=[mock.Mock(embedding=[0.9, 0.1])])

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch("openai.OpenAI", return_value=client), \
             mock.patch("server.llm_story_coverage.gather_module_sources", return_value={}):
            result = lsc.generate_story_requirements(DOC, CONTEXT, self.g, Path("/tmp"), self.index_path)

        self.assertEqual(result.requirements[0]["status"], "unknown")  # demoted, never trusted

    def test_no_checkable_criteria_returns_empty_without_any_judge_call(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = mock_chat_response({"requirements": []})

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            result = lsc.generate_story_requirements(DOC, CONTEXT, self.g, Path("/tmp"), self.index_path)

        self.assertEqual(result.requirements, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)  # decompose only

    def test_missing_api_key_raises_before_any_network_call(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                lsc.generate_story_requirements(DOC, CONTEXT, self.g, Path("/tmp"), self.index_path)
        self.assertIn("isn't configured", str(ctx.exception))

    def test_an_unbuildable_index_falls_back_to_judging_against_every_module(self):
        """If retrieval can't be built at all (no cached index AND the
        embeddings call itself fails), Story Coverage must still work --
        falling back to the old "judge against every module" behavior rather
        than refusing to run."""
        missing_index_path = Path(tempfile.mkdtemp()) / "embeddings.json"  # nothing cached here
        chat_responses = [
            mock_chat_response({"requirements": [{"text": "Users can pay by card", "type": "positive"}]}),
            mock_chat_response({"status": "supported", "covers": ["pay_now"], "rationale": "x"}),
        ]
        client = mock.Mock()
        client.chat.completions.create.side_effect = chat_responses
        client.embeddings.create.side_effect = RuntimeError("embeddings model unavailable")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch("openai.OpenAI", return_value=client), \
             mock.patch("server.llm_story_coverage.gather_module_sources") as gather_mock:
            gather_mock.return_value = {}
            result = lsc.generate_story_requirements(DOC, CONTEXT, self.g, Path("/tmp"), missing_index_path)

        narrowed_modules = gather_mock.call_args.args[2]
        self.assertEqual(set(narrowed_modules), {"checkout", "search"})  # no narrowing -- every module, same as before
        self.assertEqual(result.requirements[0]["status"], "supported")


if __name__ == "__main__":
    unittest.main()
