import unittest

from server import evidence_rules as er


class EvidenceLevelTests(unittest.TestCase):
    def test_no_citations_is_insufficient_evidence_regardless_of_status(self):
        self.assertEqual(er.evidence_level({"covers": [], "status": "supported"}), "insufficient_evidence")
        self.assertEqual(er.evidence_level({"covers": None, "status": "contradicted"}), "insufficient_evidence")

    def test_citations_plus_unknown_status_is_inconclusive(self):
        self.assertEqual(er.evidence_level({"covers": ["foo"], "status": "unknown"}), "inconclusive")

    def test_missing_status_with_citations_is_inconclusive_not_an_error(self):
        self.assertEqual(er.evidence_level({"covers": ["foo"], "status": None}), "inconclusive")

    def test_needs_human_input_with_citations_is_inconclusive(self):
        self.assertEqual(er.evidence_level({"covers": ["foo"], "status": "needs_human_input"}), "inconclusive")

    def test_citations_plus_supported_is_ai_supported(self):
        self.assertEqual(er.evidence_level({"covers": ["foo"], "status": "supported"}), "ai_supported")

    def test_citations_plus_contradicted_is_ai_conflict(self):
        self.assertEqual(er.evidence_level({"covers": ["foo"], "status": "contradicted"}), "ai_conflict")


class StalenessTests(unittest.TestCase):
    def test_no_latest_run_means_nothing_is_stale(self):
        self.assertFalse(er.is_stale({"run_id": "old"}, None))

    def test_matching_run_id_is_not_stale(self):
        self.assertFalse(er.is_stale({"run_id": "run-2"}, "run-2"))

    def test_older_run_id_is_stale(self):
        self.assertTrue(er.is_stale({"run_id": "run-1"}, "run-2"))

    def test_no_run_id_at_all_is_never_stale(self):
        # A human-confirmed row, or one predating run_id tracking, has nothing to be stale against.
        self.assertFalse(er.is_stale({"run_id": None}, "run-2"))
        self.assertFalse(er.is_stale({}, "run-2"))


class LatestReviewByTargetTests(unittest.TestCase):
    def test_empty_reviews_gives_empty_map(self):
        self.assertEqual(er.latest_review_by_target([]), {})

    def test_keeps_only_the_newest_review_per_target_given_newest_first_input(self):
        reviews = [
            {"target_id": "a", "decision": "rejected_ai_judgment", "reviewed_at": "2026-02-01"},
            {"target_id": "a", "decision": "confirmed_supported", "reviewed_at": "2026-01-01"},
            {"target_id": "b", "decision": "needs_more_info", "reviewed_at": "2026-01-15"},
        ]
        out = er.latest_review_by_target(reviews)
        self.assertEqual(out["a"]["decision"], "rejected_ai_judgment")
        self.assertEqual(out["b"]["decision"], "needs_more_info")
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
