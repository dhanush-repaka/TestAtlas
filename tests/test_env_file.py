"""The optional .env loader (server/env_file.py). The rule that matters most: it must never
override real environment variables, so the hosted site is unaffected by any file."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from server import env_file


class ParseTests(unittest.TestCase):
    def test_plain_export_quotes_comments_and_blank_lines(self):
        got = env_file.parse(
            "# a comment\n\nA=1\nexport B = two \nC='has # hash and  spaces '\nD=\"q\"\nE=val # trailing\nF=a#b\n"
            "G=\n1BAD=x\nno equals here\n=novalue\n")
        self.assertEqual(got, {"A": "1", "B": "two", "C": "has # hash and  spaces ", "D": "q", "E": "val", "F": "a#b", "G": ""})

    def test_bom_and_windows_line_endings(self):
        self.assertEqual(env_file.parse("﻿A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})

    def test_a_value_containing_equals_keeps_it(self):
        self.assertEqual(env_file.parse("KEY=abc==\nURL=postgres://u:p@h/db?x=1"), {"KEY": "abc==", "URL": "postgres://u:p@h/db?x=1"})


class LoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".env"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text, mode=0o600):
        self.path.write_text(text)
        self.path.chmod(mode)

    def test_real_environment_variables_win_over_the_file(self):
        self.write("OPENAI_API_KEY=from-file\nTESTATLAS_PASSWORD=file-pw\nDATA_ROOT=/from/file\n")
        env = {"OPENAI_API_KEY": "from-platform-secret"}
        loaded = env_file.load_env_file(self.path, env)
        self.assertEqual(env["OPENAI_API_KEY"], "from-platform-secret")     # not overridden
        self.assertEqual(env["TESTATLAS_PASSWORD"], "file-pw")               # only what was missing
        self.assertEqual(sorted(loaded), ["DATA_ROOT", "TESTATLAS_PASSWORD"])

    def test_an_empty_real_value_is_still_a_real_value(self):
        self.write("TESTATLAS_PASSWORD=file-pw\n")
        env = {"TESTATLAS_PASSWORD": ""}
        env_file.load_env_file(self.path, env)
        self.assertEqual(env["TESTATLAS_PASSWORD"], "")

    def test_missing_or_unreadable_file_is_a_no_op(self):
        env = {"A": "1"}
        self.assertEqual(env_file.load_env_file(Path(self._tmp.name) / "nope", env), [])
        self.assertEqual(env_file.load_env_file(Path(self._tmp.name), env), [])   # a directory
        self.assertEqual(env, {"A": "1"})

    def test_env_file_variable_chooses_the_file_and_empty_disables_it(self):
        self.write("X=1\n")
        env = {"TESTATLAS_ENV_FILE": str(self.path)}
        env_file.load_env_file(environ=env)
        self.assertEqual(env["X"], "1")
        off = {"TESTATLAS_ENV_FILE": ""}
        self.assertEqual(env_file.load_env_file(environ=off), [])
        self.assertEqual(off, {"TESTATLAS_ENV_FILE": ""})

    def test_values_are_never_printed_only_names(self):
        import contextlib, io
        self.write("OPENAI_API_KEY=sk-super-secret-value\n", mode=0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            env_file.load_env_file(self.path, {})
        out = err.getvalue()
        self.assertIn("OPENAI_API_KEY", out)
        self.assertNotIn("sk-super-secret-value", out)
        if os.name == "posix":
            self.assertIn("chmod 600", out)                                   # world-readable secrets file is flagged


class ProjectSetupTests(unittest.TestCase):
    """Guards the packaging rules that keep secrets out of the hosted image and the repo."""
    ROOT = Path(__file__).resolve().parent.parent

    def test_env_is_kept_out_of_the_docker_image_and_git_but_the_example_is_not(self):
        docker = (self.ROOT / ".dockerignore").read_text().splitlines()
        git = (self.ROOT / ".gitignore").read_text().splitlines()
        for lines in (docker, git):
            self.assertIn(".env", lines)
            self.assertIn(".env.*", lines)
            self.assertIn("!.env.example", lines)

    def test_example_documents_every_setting_the_app_reads_and_holds_no_secrets(self):
        text = (self.ROOT / ".env.example").read_text()
        for name in ("TESTATLAS_PASSWORD", "TESTATLAS_SECRET", "OPENAI_API_KEY", "OPENAI_TEST_MODEL", "OPENAI_BASE_URL",
                     "DATA_ROOT", "BASE_PATH", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
            self.assertIn(name, text)
        for key, value in env_file.parse(text).items():
            self.assertEqual(value, "", f"{key} in .env.example must not carry a value")


if __name__ == "__main__":
    unittest.main()
