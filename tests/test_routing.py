"""Server-side half of deep linking: the open-redirect guard on login's `next`
(server/auth.py's safe_next_path), the login form round-tripping it safely, and
the SPA catch-all (server/app.py's spa_fallback) that lets a repo/tab URL survive
a hard reload or a shared link instead of 404ing. The matching client-side router
lives in static/app.js and isn't covered here (no JS test runner in this repo) --
exercised manually instead (see the session this was built in).
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import auth, db
from server.app import BASE_PATH, app

HTML = {"accept": "text/html"}


class SafeNextPathTests(unittest.TestCase):
    def test_missing_or_empty_falls_back_to_the_app_root(self):
        self.assertEqual(auth.safe_next_path(None, ""), "/")
        self.assertEqual(auth.safe_next_path("", "/testatlas"), "/testatlas/")

    def test_a_same_app_path_is_accepted(self):
        self.assertEqual(auth.safe_next_path("/repo/abc/graph", ""), "/repo/abc/graph")
        self.assertEqual(auth.safe_next_path("/testatlas/repo/abc/graph", "/testatlas"), "/testatlas/repo/abc/graph")

    def test_another_host_is_rejected_scheme_or_protocol_relative(self):
        # An unauthenticated visitor's own `next` is attacker-controlled -- a crafted
        # login link must never be able to redirect a freshly authenticated session
        # off this app, however it tries to spell another host.
        for evil in ("https://evil.example/x", "//evil.example/x", "http://evil.example"):
            self.assertEqual(auth.safe_next_path(evil, ""), "/", evil)
            self.assertEqual(auth.safe_next_path(evil, "/testatlas"), "/testatlas/", evil)

    def test_a_path_outside_this_apps_own_prefix_is_rejected(self):
        # Under a sub-path deployment, a bare "/elsewhere" isn't this app's own route
        # even though it does start with "/" -- only "{base_path}/..." is honored.
        self.assertEqual(auth.safe_next_path("/elsewhere", "/testatlas"), "/testatlas/")


class SpaFallbackTests(unittest.TestCase):
    """No TESTATLAS_PASSWORD here -- the auth gate is a no-op, isolating the routing
    question (does this path resolve to the SPA shell at all?) from the login flow,
    which SafeNextPathTests and LoginNextRoundTripTests cover on their own."""

    def setUp(self):
        self._patch = mock.patch.dict(os.environ, {}, clear=False)
        self._patch.start()
        os.environ.pop("TESTATLAS_PASSWORD", None)
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self._patch.stop()

    def test_known_routes_are_unaffected_by_the_catch_all(self):
        self.assertEqual(self.client.get("/", headers=HTML).status_code, 200)
        self.assertEqual(self.client.get("/api/system/config", headers=HTML).status_code, 200)
        self.assertEqual(self.client.get("/static/app.js", headers=HTML).status_code, 200)
        self.assertEqual(self.client.get("/login", headers=HTML).status_code, 200)

    def test_a_repo_tab_url_serves_the_same_spa_shell_as_the_root(self):
        root = self.client.get("/", headers=HTML)
        deep = self.client.get("/repo/does-not-exist/graph", headers=HTML)
        self.assertEqual(deep.status_code, 200)
        self.assertEqual(deep.text, root.text)  # identical shell either way -- the client router sorts out the rest
        self.assertIn("app.js", deep.text)

    def test_deeply_nested_or_trailing_slash_paths_also_resolve(self):
        for path in ("/repo/abc/graph/", "/repo/abc", "/whatever/this/app/doesnt/define"):
            self.assertEqual(self.client.get(path, headers=HTML).status_code, 200, path)


class LoginNextRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_patches = [mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
                            mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db")]
        for p in self._db_patches:
            p.start()
        db.init_db()
        self._env_patch = mock.patch.dict(os.environ, {"TESTATLAS_PASSWORD": "pw123"})
        self._env_patch.start()
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self._env_patch.stop()
        for p in self._db_patches:
            p.stop()
        self._tmp.cleanup()

    def test_an_unauthenticated_deep_link_is_carried_through_login_and_back(self):
        redirected = self.client.get("/repo/abc/graph", headers=HTML)
        self.assertEqual(redirected.status_code, 307)
        self.assertEqual(redirected.headers["location"], f"{BASE_PATH}/login?next=%2Frepo%2Fabc%2Fgraph")

        form_page = self.client.get(redirected.headers["location"], headers=HTML)
        self.assertIn('<input type="hidden" name="next" value="/repo/abc/graph">', form_page.text)

        submitted = self.client.post("/login", data={"password": "pw123", "next": "/repo/abc/graph"})
        self.assertEqual(submitted.status_code, 303)
        self.assertEqual(submitted.headers["location"], "/repo/abc/graph")  # the ORIGINAL destination, not the dashboard
        self.assertIn(auth.COOKIE_NAME, submitted.cookies)

    def test_a_wrong_password_keeps_next_for_the_retry(self):
        r = self.client.post("/login", data={"password": "nope", "next": "/repo/abc/graph"})
        self.assertEqual(r.headers["location"], f"{BASE_PATH}/login?error=1&next=%2Frepo%2Fabc%2Fgraph")

    def test_a_malicious_next_never_escapes_this_app_even_with_the_right_password(self):
        r = self.client.post("/login", data={"password": "pw123", "next": "https://evil.example"})
        self.assertEqual(r.headers["location"], f"{BASE_PATH}/")

    def test_the_hidden_next_field_is_html_escaped(self):
        page = self.client.get("/login", params={"next": '"><script>alert(1)</script>'}, headers=HTML)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)


if __name__ == "__main__":
    unittest.main()
