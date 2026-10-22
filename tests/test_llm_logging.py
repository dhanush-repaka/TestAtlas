"""Tests for llm_logging.py -- the shared latency/token-usage wrapper every
LLM call site in this app goes through. The one property that matters most:
it must never change what a call site sees -- the real return value on
success, the exact same exception on failure -- logging is purely a side
channel.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llm_logging as ll


class CallAndLogTests(unittest.TestCase):
    def setUp(self):
        # Override the module-level opt-out (set globally by tests/__init__.py)
        # just for this test class, since it exists specifically to test logging.
        self._env_patch = mock.patch.dict(os.environ, {"TESTATLAS_DISABLE_LLM_LOG": ""})
        self._env_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._log_path_patch = mock.patch.object(ll, "_LOG_PATH", Path(self._tmp.name) / "llm_calls.log")
        self._log_path_patch.start()

    def tearDown(self):
        self._log_path_patch.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _read_log_lines(self) -> list[dict]:
        if not ll._LOG_PATH.exists():
            return []
        return [json.loads(l) for l in ll._LOG_PATH.read_text().splitlines()]

    def test_returns_the_wrapped_calls_result_unchanged(self):
        result = ll.call_and_log("site.x", "gpt-4o-mini", lambda: {"ok": "value"})
        self.assertEqual(result, {"ok": "value"})

    def test_an_exception_propagates_unchanged(self):
        def boom():
            raise ValueError("specific message")
        with self.assertRaises(ValueError) as ctx:
            ll.call_and_log("site.x", "gpt-4o-mini", boom)
        self.assertEqual(str(ctx.exception), "specific message")

    def test_a_successful_call_logs_latency_and_usage(self):
        response = mock.Mock(usage=mock.Mock(prompt_tokens=100, completion_tokens=20, total_tokens=120))
        ll.call_and_log("site.success", "gpt-4o-mini", lambda: response, prompt_chars=42)
        lines = self._read_log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["site"], "site.success")
        self.assertEqual(lines[0]["model"], "gpt-4o-mini")
        self.assertEqual(lines[0]["prompt_chars"], 42)
        self.assertTrue(lines[0]["ok"])
        self.assertEqual(lines[0]["usage"], {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
        self.assertIn("latency_ms", lines[0])
        self.assertGreaterEqual(lines[0]["latency_ms"], 0)

    def test_a_failed_call_logs_the_error_and_still_has_a_latency(self):
        def boom():
            raise RuntimeError("rate limited")
        with self.assertRaises(RuntimeError):
            ll.call_and_log("site.failure", "gpt-4o-mini", boom)
        lines = self._read_log_lines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["ok"])
        self.assertEqual(lines[0]["error"], "rate limited")
        self.assertIn("latency_ms", lines[0])

    def test_a_response_with_no_usage_attribute_logs_none_not_an_error(self):
        response = mock.Mock(spec=[])  # no .usage attribute at all
        ll.call_and_log("site.no_usage", "gpt-4o-mini", lambda: response)
        lines = self._read_log_lines()
        self.assertIsNone(lines[0]["usage"])

    def test_disable_flag_suppresses_both_the_file_write_and_the_return_value_is_untouched(self):
        with mock.patch.dict(os.environ, {"TESTATLAS_DISABLE_LLM_LOG": "1"}):
            result = ll.call_and_log("site.disabled", "gpt-4o-mini", lambda: "the real result")
        self.assertEqual(result, "the real result")
        self.assertEqual(self._read_log_lines(), [])

    def test_read_recent_returns_newest_first(self):
        ll.call_and_log("site.first", "m", lambda: mock.Mock(spec=[]))
        ll.call_and_log("site.second", "m", lambda: mock.Mock(spec=[]))
        recent = ll.read_recent()
        self.assertEqual([r["site"] for r in recent], ["site.second", "site.first"])

    def test_read_recent_with_no_file_yet_returns_empty(self):
        self.assertEqual(ll.read_recent(), [])


if __name__ == "__main__":
    unittest.main()
