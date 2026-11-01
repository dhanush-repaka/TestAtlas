"""Tests for the human-review infrastructure (Stage 2/3/5 of the
false-confidence reduction plan): server/db.py's human_reviews table,
the extended .../confirm route, the new per-test-case .../review route,
and the .../review-accuracy endpoint. Run with:
  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import db
from server.app import app


class HumanReviewsApiTestCase(unittest.TestCase):
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
            "class Cart:\n    def add(self, item):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]
        self.client.post(f"/api/repos/{self.repo_id}/run")
        doc = self.client.post(f"/api/repos/{self.repo_id}/documents", json={"name": "story", "content": "x"}).json()
        self.doc_id = doc["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_requirement(self, status="needs_human_input", covers=None):
        r = db.replace_story_requirements(self.repo_id, self.doc_id, None, [
            {"text": "x", "type": "positive", "status": status, "covers": covers or []},
        ])
        return db.list_story_requirements(self.repo_id, self.doc_id)[0]

    def _make_case(self, status=None, covers=None):
        case = db.create_test_case(self.repo_id, "cart", None, {
            "title": "Add an item", "category": "happy_path",
            "steps": [{"action": "a", "expected": "b"}], "expected_result": "b",
        })
        if status is not None:
            db.update_test_case_verification(case["id"], status, "r", covers or [], "cart", assessed_model="gpt-4o-mini")
        return db.list_test_cases(self.repo_id)[0]


class ConfirmRequirementRecordsReviewTests(HumanReviewsApiTestCase):
    def test_confirming_supported_records_a_confirmed_supported_review(self):
        req = self._make_requirement()
        r = self.client.post(
            f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm",
            json={"status": "supported", "rationale": "looks right"},
        )
        self.assertEqual(r.status_code, 200)
        reviews = db.list_human_reviews(self.repo_id)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["target_type"], "story_requirement")
        self.assertEqual(reviews[0]["target_id"], req["id"])
        self.assertEqual(reviews[0]["decision"], "confirmed_supported")

    def test_confirming_contradicted_records_a_confirmed_contradicted_review(self):
        req = self._make_requirement()
        self.client.post(
            f"/api/repos/{self.repo_id}/story-requirements/{req['id']}/confirm",
            json={"status": "contradicted"},
        )
        reviews = db.list_human_reviews(self.repo_id)
        self.assertEqual(reviews[0]["decision"], "confirmed_contradicted")

    def test_confirming_an_unknown_requirement_404s_without_recording_a_review(self):
        r = self.client.post(
            f"/api/repos/{self.repo_id}/story-requirements/does-not-exist/confirm",
            json={"status": "supported"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(db.list_human_reviews(self.repo_id), [])


class TestCaseReviewRouteTests(HumanReviewsApiTestCase):
    def test_records_a_review_without_touching_the_cases_own_status(self):
        case = self._make_case(status="supported", covers=["Cart.add"])
        r = self.client.post(
            f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review",
            json={"decision": "rejected_ai_judgment", "note": "the step doesn't actually exercise this"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["target_type"], "test_case")
        self.assertEqual(body["decision"], "rejected_ai_judgment")
        self.assertEqual(body["note"], "the step doesn't actually exercise this")
        # The AI's own judgment is untouched -- the review sits alongside it.
        reloaded = next(c for c in db.list_test_cases(self.repo_id) if c["id"] == case["id"])
        self.assertEqual(reloaded["status"], "supported")

    def test_an_invalid_decision_is_rejected(self):
        case = self._make_case(status="supported", covers=["Cart.add"])
        r = self.client.post(
            f"/api/repos/{self.repo_id}/test-cases/{case['id']}/review",
            json={"decision": "yolo"},
        )
        self.assertEqual(r.status_code, 400)

    def test_reviewing_an_unknown_case_404s(self):
        r = self.client.post(
            f"/api/repos/{self.repo_id}/test-cases/does-not-exist/review",
            json={"decision": "confirmed_supported"},
        )
        self.assertEqual(r.status_code, 404)


class ReviewAccuracyEndpointTests(HumanReviewsApiTestCase):
    def test_empty_state_with_no_reviews_yet(self):
        r = self.client.get(f"/api/repos/{self.repo_id}/review-accuracy")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body, {
            "total_reviews": 0, "agreed_with_ai": 0, "false_support_count": 0,
            "false_conflict_count": 0, "needs_more_info_count": 0, "agreement_rate_pct": None,
        })

    def test_unknown_repo_404s(self):
        r = self.client.get("/api/repos/does-not-exist/review-accuracy")
        self.assertEqual(r.status_code, 404)

    def test_agreement_false_support_and_false_conflict_are_counted_separately(self):
        agreed_case = self._make_case(status="supported", covers=["Cart.add"])
        false_support_case = self._make_case(status="supported", covers=["Cart.add"])
        false_conflict_case = self._make_case(status="contradicted", covers=["Cart.add"])
        unsure_case = self._make_case(status="supported", covers=["Cart.add"])

        self.client.post(f"/api/repos/{self.repo_id}/test-cases/{agreed_case['id']}/review",
                          json={"decision": "confirmed_supported"})
        self.client.post(f"/api/repos/{self.repo_id}/test-cases/{false_support_case['id']}/review",
                          json={"decision": "rejected_ai_judgment"})
        self.client.post(f"/api/repos/{self.repo_id}/test-cases/{false_conflict_case['id']}/review",
                          json={"decision": "rejected_ai_judgment"})
        self.client.post(f"/api/repos/{self.repo_id}/test-cases/{unsure_case['id']}/review",
                          json={"decision": "needs_more_info"})

        body = self.client.get(f"/api/repos/{self.repo_id}/review-accuracy").json()
        self.assertEqual(body["total_reviews"], 4)
        self.assertEqual(body["agreed_with_ai"], 1)
        self.assertEqual(body["false_support_count"], 1)
        self.assertEqual(body["false_conflict_count"], 1)
        self.assertEqual(body["needs_more_info_count"], 1)
        self.assertEqual(body["agreement_rate_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()
