"""Tests for plain-English module naming (server/llm_module_naming.py) and its
storage. OpenAI is mocked -- these check what we control: the clues and
instructions sent, how a large repo is batched, and what we accept back.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import db
from server import llm_module_naming as naming


def mod(name, files=None, **extra):
    return {"module": name, "files": files or [{"file": f"{name}.x", "purpose": None, "classes": [], "functions": ["f"]}], **extra}


CART = {"module": "components.cart", "files": [
    {"file": "components.cart.add-to-cart", "purpose": None, "classes": [], "functions": ["AddToCart", "SubmitButton"],
     "ui_text": ["Add To Cart", "Out Of Stock"]},
    {"file": "components.cart.modal", "purpose": "The cart drawer", "classes": [{"name": "CartModal", "methods": ["open"]}],
     "functions": [], "ui_text": ["My Cart", "Out Of Stock", "Your cart is empty."]},
]}


class SummaryTests(unittest.TestCase):
    def test_clues_are_the_things_that_reveal_what_a_user_sees(self):
        s = naming._summarize_modules([CART])[0]
        self.assertEqual(s["files"], ["add-to-cart", "modal"])             # leaf names, module prefix stripped
        self.assertEqual(s["ui_text"], ["Add To Cart", "Out Of Stock", "My Cart", "Your cart is empty."])  # deduped, in order
        self.assertEqual(s["purpose"], "The cart drawer")
        self.assertIn("AddToCart", s["sample_names"])

    def test_empty_clues_are_omitted_and_caps_hold(self):
        bare = naming._summarize_modules([mod("lib.util")])[0]
        self.assertNotIn("ui_text", bare)
        self.assertNotIn("purpose", bare)
        many = {"module": "m", "files": [{"file": f"m.f{i}", "purpose": None, "classes": [], "functions": [f"fn{i}"],
                                            "ui_text": [f"Label number {i}"]} for i in range(30)]}
        s = naming._summarize_modules([many])[0]
        self.assertEqual((len(s["files"]), len(s["sample_names"]), len(s["ui_text"])), (8, 8, 10))


class PromptTests(unittest.TestCase):
    def test_prompt_asks_for_screens_and_features_not_technical_names(self):
        p = naming._build_prompt(naming._summarize_modules([CART]), ["components.cart", "lib.shopify.queries"])
        for needle in ("NON-TECHNICAL", "Shopping Cart", "the SCREEN or FEATURE", "NEVER use developer words", "HOME PAGE", "name the JOB",
                       '"kind"', "supporting", '"description"', "ui_text", "Add To Cart", "lib.shopify.queries"):
            self.assertIn(needle, p)


class GenerateTests(unittest.TestCase):
    def _run(self, responses, modules):
        def resp(payload):
            r = mock.Mock()
            r.choices = [mock.Mock(message=mock.Mock(content=json.dumps(payload)))]
            return r
        client = mock.Mock()
        client.chat.completions.create.side_effect = [resp(p) for p in responses]
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            return naming.generate_module_names({"modules": modules}), client

    def test_returns_name_kind_and_description_and_defaults_bad_kinds_to_feature(self):
        out, _ = self._run([{"modules": {
            "components.cart": {"name": " Shopping Cart ", "kind": "feature", "description": "Add and remove items."},
            "lib.util": {"name": "Store Connection", "kind": "supporting", "description": "Talks to the store."},
            "components.x": {"name": "Odd One", "kind": "banana"},
            "components.y": "Plain Name",
        }}], [CART, mod("lib.util"), mod("components.x"), mod("components.y")])
        self.assertEqual(out["components.cart"], {"name": "Shopping Cart", "kind": "feature", "description": "Add and remove items."})
        self.assertEqual(out["lib.util"]["kind"], "supporting")
        self.assertEqual(out["components.x"], {"name": "Odd One", "kind": "feature", "description": None})  # a hidden module is worse than a shown one
        self.assertEqual(out["components.y"]["name"], "Plain Name")

    def test_invented_or_unusable_entries_are_dropped(self):
        out, _ = self._run([{"modules": {
            "components.cart": {"name": "Shopping Cart"}, "not.a.real.module": {"name": "Invented"},
            "lib.util": {"name": "  "}, "lib.other": {"kind": "feature"}}}], [CART, mod("lib.util"), mod("lib.other")])
        self.assertEqual(list(out), ["components.cart"])

    def test_a_large_repo_is_batched_and_every_batch_sees_the_whole_app(self):
        modules = [mod(f"pkg.m{i}") for i in range(130)]
        responses = [{"modules": {m["module"]: {"name": f"Name {m['module']}"} for m in modules[i:i + 60]}} for i in range(0, 130, 60)]
        out, client = self._run(responses, modules)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 3)                                        # 60 + 60 + 10
        self.assertEqual(len(out), 130)
        for call in calls:                                                     # the full outline goes into EVERY batch
            prompt = call.kwargs["messages"][0]["content"]
            self.assertIn('"pkg.m0"', prompt.split("MODULES TO NAME")[0])
            self.assertIn('"pkg.m129"', prompt.split("MODULES TO NAME")[0])
            self.assertLessEqual(call.kwargs["max_tokens"], 16384)

    def test_developer_sounding_names_are_re_asked_once_and_only_those(self):
        out, client = self._run([
            {"modules": {"components.cart": {"name": "Shopping Cart", "kind": "feature"},
                         "lib.shopify.queries": {"name": "Shopify Queries", "kind": "supporting"},
                         "next.config": {"name": "Next.js Configuration", "kind": "supporting"}}},
            {"modules": {"lib.shopify.queries": {"name": "Product Data Retrieval", "kind": "supporting"},
                         "next.config": {"name": "Site Settings", "kind": "supporting"},
                         "components.cart": {"name": "Should Not Overwrite", "kind": "feature"}}},
        ], [CART, mod("lib.shopify.queries"), mod("next.config")])
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        follow_up = calls[1].kwargs["messages"][0]["content"]
        self.assertIn('"lib.shopify.queries" is currently called "Shopify Queries"', follow_up)
        self.assertNotIn("is currently called \"Shopping Cart\"", follow_up)
        self.assertEqual(out["components.cart"]["name"], "Shopping Cart")      # a clean name is never re-asked
        self.assertEqual(out["lib.shopify.queries"]["name"], "Product Data Retrieval")
        self.assertEqual(out["next.config"]["name"], "Site Settings")

    def test_the_root_route_folder_with_a_page_file_is_the_home_page(self):
        root = mod("app", [{"file": "app.page", "purpose": None, "classes": [], "functions": ["HomePage"]}])
        nested = mod("app.search", [{"file": "app.search.page", "purpose": None, "classes": [], "functions": ["SearchPage"]}])
        no_page = mod("app", [{"file": "app.robots", "purpose": None, "classes": [], "functions": ["robots"]}])
        out, _ = self._run([{"modules": {"app": {"name": "Site Layout", "kind": "supporting"},
                                         "app.search": {"name": "Search Page", "kind": "feature"}}}], [root, nested])
        self.assertEqual((out["app"]["name"], out["app"]["kind"]), ("Home Page", "feature"))
        self.assertEqual(out["app.search"]["name"], "Search Page")             # only the ROOT route folder
        out, _ = self._run([{"modules": {"app": {"name": "Crawler Files", "kind": "supporting"}}}], [no_page])
        self.assertEqual(out["app"]["name"], "Crawler Files")                  # no page file, no home page

    def test_no_second_call_when_every_name_is_plain(self):
        _, client = self._run([{"modules": {"components.cart": {"name": "Shopping Cart"}}}], [CART])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_dev_speak_check(self):
        for bad in ("Components", "Shopify Queries", "Library", "Application", "Next.js Configuration", "API Routes"):
            self.assertTrue(naming._dev_speak(bad), bad)
        for good in ("Home Page", "Shopping Cart", "Store Connection", "Search & Filters", "Icons & Graphics", "Page Footer"):
            self.assertFalse(naming._dev_speak(good), good)

    def test_missing_key_and_bad_responses_raise_a_message_safe_to_show(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(RuntimeError):
                naming.generate_module_names({"modules": [CART]})
        r = mock.Mock(); r.choices = [mock.Mock(message=mock.Mock(content="not json"))]
        client = mock.Mock(); client.chat.completions.create.return_value = r
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "valid JSON"):
                naming.generate_module_names({"modules": [CART]})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
                         mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db")]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_round_trip_and_plain_string_labels_still_work(self):
        db.init_db()
        rid = db.create_repo({"name": "r", "source_type": "local", "local_path": "/tmp"})["id"]
        db.set_module_labels(rid, {"components.cart": {"name": "Shopping Cart", "kind": "feature", "description": "Add items."},
                                   "lib.q": {"name": "Store Connection", "kind": "supporting", "description": None},
                                   "old.style": "Just A Name"})
        d = db.get_module_label_details(rid)
        self.assertEqual(d["components.cart"], {"name": "Shopping Cart", "kind": "feature", "description": "Add items."})
        self.assertEqual(d["lib.q"]["kind"], "supporting")
        self.assertEqual(d["old.style"], {"name": "Just A Name", "kind": "feature", "description": None})
        self.assertEqual(db.get_module_labels(rid)["components.cart"], "Shopping Cart")   # the plain-name view still works

    def test_a_table_created_by_the_previous_version_is_migrated_in_place(self):
        # what production has: module_labels WITHOUT kind/description, holding a real row
        (Path(self._tmp.name)).mkdir(exist_ok=True)
        conn = sqlite3.connect(Path(self._tmp.name) / "kg.db")
        conn.executescript("""
            CREATE TABLE repos (id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
                framework TEXT NOT NULL DEFAULT 'python_devcode', local_path TEXT, ado_org TEXT, ado_project TEXT, ado_repo TEXT,
                ado_branch TEXT DEFAULT 'main', ado_pat_enc TEXT, github_owner TEXT, github_repo TEXT,
                github_branch TEXT DEFAULT 'main', github_pat_enc TEXT, created_at TEXT NOT NULL);
            CREATE TABLE module_labels (repo_id TEXT NOT NULL, module TEXT NOT NULL, display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY (repo_id, module));
            INSERT INTO repos (id, name, source_type, created_at) VALUES ('r1', 'old', 'local', 'now');
            INSERT INTO module_labels VALUES ('r1', 'kg', 'Knowledge Graph Engine', 'now');""")
        conn.commit(); conn.close()
        db.init_db()   # must add the columns without losing the row
        self.assertEqual(db.get_module_label_details("r1")["kg"],
                         {"name": "Knowledge Graph Engine", "kind": "feature", "description": None})


if __name__ == "__main__":
    unittest.main()
