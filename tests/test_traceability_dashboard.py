"""Tests for server/traceability_dashboard.py -- the project-status dashboard's
pure-computation rollup (user story <-> code <-> test case), and server/db.py's
dashboard_snapshots trend-storage functions.
Run with:  python -m unittest discover -s tests -t ."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import db, traceability_dashboard as td
from server.app import app

DOC_A = {"id": "doc-a", "name": "5111-inventory-reservation"}
DOC_B = {"id": "doc-b", "name": "5112-account-notifications"}


def req(id, doc_id, status, covers=None, **over):
    base = {"id": id, "doc_id": doc_id, "text": f"requirement {id}", "type": "positive",
            "status": status, "covers": covers or [], "rationale": None, "question": None}
    base.update(over)
    return base


def case(id, **over):
    base = {"id": id, "title": f"case {id}", "covers": [], "module": None,
            "source_doc_id": None, "source_requirement_id": None, "source_journey_id": None}
    base.update(over)
    return base


class ComputeSummaryRequirementsTests(unittest.TestCase):
    def test_status_breakdown_counts_every_requirement_once(self):
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-a", "contradicted"),
                         req("r3", "doc-b", "unknown"), req("r4", "doc-b", "needs_human_input")]
        out = td.compute_summary([DOC_A, DOC_B], requirements, [], [], {})
        self.assertEqual(out["requirements"]["total"], 4)
        self.assertEqual(out["requirements"]["by_status"],
                         {"supported": 1, "contradicted": 1, "unknown": 1, "needs_human_input": 1})

    def test_contradiction_and_supported_rate_are_percentages_of_the_total(self):
        requirements = [req("r1", "doc-a", "contradicted"), req("r2", "doc-a", "supported"),
                         req("r3", "doc-a", "supported"), req("r4", "doc-a", "unknown")]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertEqual(out["requirements"]["contradiction_rate"], 25.0)
        self.assertEqual(out["requirements"]["supported_rate"], 50.0)

    def test_rates_are_zero_not_an_error_with_no_requirements_at_all(self):
        out = td.compute_summary([DOC_A], [], [], [], {})
        self.assertEqual(out["requirements"]["contradiction_rate"], 0.0)
        self.assertEqual(out["requirements"]["supported_rate"], 0.0)

    def test_contradicted_and_needs_human_input_lists_carry_the_document_name(self):
        requirements = [req("r1", "doc-a", "contradicted", rationale="stock goes negative"),
                         req("r2", "doc-b", "needs_human_input", question="what's the limit?")]
        out = td.compute_summary([DOC_A, DOC_B], requirements, [], [], {})
        self.assertEqual(out["gaps"]["contradicted_requirements"],
                         [{"id": "r1", "doc_id": "doc-a", "doc_name": "5111-inventory-reservation",
                           "text": "requirement r1", "rationale": "stock goes negative", "question": None,
                           "touches_critical_code": False}])
        self.assertEqual(out["gaps"]["needs_human_input_requirements"][0]["question"], "what's the limit?")


class ComputeSummaryDocumentCoverageTests(unittest.TestCase):
    def test_a_document_with_no_requirements_decomposed_yet_is_a_gap(self):
        out = td.compute_summary([DOC_A], [], [], [], {})
        self.assertEqual(out["documents"]["with_requirements"], 0)
        self.assertEqual(out["documents"]["without_requirements"], 1)
        self.assertEqual(out["gaps"]["documents_not_analyzed"], [{"id": "doc-a", "name": "5111-inventory-reservation"}])

    def test_a_document_is_coded_once_any_requirement_has_a_real_citation(self):
        # "unknown" always means no citation was found at all (the judge never
        # reaches a verdict without something to cite) -- a non-empty covers list,
        # regardless of whether the verdict ends up supported or contradicted, is
        # what "this story has real code behind it" actually means.
        requirements = [req("r1", "doc-a", "contradicted", covers=["Inventory.reserve"]),
                         req("r2", "doc-a", "unknown", covers=[])]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertTrue(out["document_rollup"][0]["has_code"])
        self.assertEqual(out["documents"]["with_code"], 1)
        self.assertEqual(out["documents"]["coded_pct"], 100.0)

    def test_a_document_with_only_unknown_requirements_is_not_coded(self):
        requirements = [req("r1", "doc-a", "unknown", covers=[])]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertFalse(out["document_rollup"][0]["has_code"])
        self.assertEqual(out["documents"]["without_code"], 1)
        self.assertEqual(out["documents"]["coded_pct"], 0.0)
        self.assertEqual(out["gaps"]["documents_without_code"], [{"id": "doc-a", "name": "5111-inventory-reservation"}])

    def test_a_document_with_zero_requirements_is_not_double_counted_as_a_code_gap(self):
        # Already covered by documents_not_analyzed -- listing it again under
        # documents_without_code would just be noise, the same gap stated twice.
        out = td.compute_summary([DOC_A], [], [], [], {})
        self.assertEqual(out["gaps"]["documents_without_code"], [])

    def test_citation_overlap_alone_marks_a_document_tested(self):
        requirements = [req("r1", "doc-a", "supported", covers=["Inventory.reserve"])]
        cases = [case("c1", covers=["Inventory.reserve"], module="inventory")]
        out = td.compute_summary([DOC_A], requirements, cases, [], {})
        doc = out["document_rollup"][0]
        self.assertTrue(doc["has_test_coverage"])
        self.assertEqual(doc["tested_count"], 1)

    def test_a_direct_source_doc_id_link_marks_a_document_tested_even_with_empty_covers(self):
        # A whole-document-generated story case is stored with covers=[] (genuinely
        # unverified) -- citation overlap alone would never catch it, so the direct
        # source_doc_id link has to be checked independently.
        requirements = [req("r1", "doc-a", "supported", covers=[])]
        cases = [case("c1", covers=[], source_doc_id="doc-a")]
        out = td.compute_summary([DOC_A], requirements, cases, [], {})
        doc = out["document_rollup"][0]
        self.assertTrue(doc["has_test_coverage"])
        self.assertEqual(doc["tested_count"], 0)  # the citation-overlap path still correctly sees no match
        self.assertEqual(out["documents"]["with_test_coverage"], 1)

    def test_a_document_with_requirements_but_no_matching_case_has_no_coverage(self):
        requirements = [req("r1", "doc-a", "unknown", covers=["Inventory.reserve"])]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        doc = out["document_rollup"][0]
        self.assertFalse(doc["has_test_coverage"])
        self.assertEqual(out["gaps"]["documents_without_tests"], [{"id": "doc-a", "name": "5111-inventory-reservation"}])

    def test_a_code_driven_case_sharing_a_citation_counts_toward_that_documents_modules(self):
        # The case was never generated FROM this document, but Story Coverage's own
        # heuristic ties it there by a shared citation -- the document's module list
        # should reflect that, same signal the export/Test Cases tab already surface.
        requirements = [req("r1", "doc-a", "supported", covers=["Inventory.reserve"])]
        cases = [case("c1", covers=["Inventory.reserve"], module="inventory")]
        out = td.compute_summary([DOC_A], requirements, cases, [], {})
        self.assertEqual(out["document_rollup"][0]["modules"], ["inventory"])


class ComputeSummaryStoryHealthTests(unittest.TestCase):
    """The dashboard's headline view: one of six mutually-exclusive buckets per
    story, most-severe-first, product-owner-facing (not test-case origin, which
    this dashboard deliberately never surfaces -- see the module docstring)."""

    def _health(self, requirements):
        return td.compute_summary([DOC_A], requirements, [], [], {})["document_rollup"][0]["health"]

    def test_no_requirements_decomposed_yet_is_not_analyzed(self):
        self.assertEqual(self._health([]), "not_analyzed")

    def test_even_one_contradicted_requirement_makes_the_whole_story_broken(self):
        # Most severe wins -- three correct criteria and one wrong one is still a
        # broken story, not a mostly-fine one; a product owner needs to see that.
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-a", "supported"),
                         req("r3", "doc-a", "supported"), req("r4", "doc-a", "contradicted")]
        self.assertEqual(self._health(requirements), "broken")

    def test_needs_human_input_outranks_not_started_but_not_broken(self):
        self.assertEqual(self._health([req("r1", "doc-a", "needs_human_input")]), "needs_clarification")
        requirements = [req("r1", "doc-a", "contradicted"), req("r2", "doc-a", "needs_human_input")]
        self.assertEqual(self._health(requirements), "broken")  # contradicted still wins

    def test_every_criterion_unknown_is_not_started(self):
        requirements = [req("r1", "doc-a", "unknown"), req("r2", "doc-a", "unknown")]
        self.assertEqual(self._health(requirements), "not_started")

    def test_a_mix_of_supported_and_unknown_is_partially_built(self):
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-a", "unknown")]
        self.assertEqual(self._health(requirements), "partially_built")

    def test_every_criterion_supported_is_verified(self):
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-a", "supported")]
        self.assertEqual(self._health(requirements), "verified")

    def test_by_health_counts_sum_to_the_total_story_count(self):
        docs = [DOC_A, DOC_B, {"id": "doc-c", "name": "c"}]
        requirements = [req("r1", "doc-a", "contradicted"), req("r2", "doc-b", "supported")]  # doc-c gets none
        out = td.compute_summary(docs, requirements, [], [], {})
        self.assertEqual(sum(out["documents"]["by_health"].values()), 3)
        self.assertEqual(out["documents"]["by_health"], {
            "not_analyzed": 1, "broken": 1, "needs_clarification": 0,
            "not_started": 0, "partially_built": 0, "verified": 1,
        })
        self.assertEqual(out["documents"]["verified_pct"], round(1 / 3 * 100, 1))


class ComputeSummaryCriticalityTests(unittest.TestCase):
    """A story "touches critical code" when one of its modules is among the
    repo's top-3-by-PageRank -- the same structural-centrality signal
    static/app.js's repoTileHtml already calls "most critical" elsewhere in
    this app, reused here rather than inventing a second notion of "critical"."""

    def test_only_the_top_3_modules_by_pagerank_count_as_critical(self):
        module_scores = [{"module": "a", "pagerank": 0.9}, {"module": "b", "pagerank": 0.5},
                          {"module": "c", "pagerank": 0.3}, {"module": "d", "pagerank": 0.1}]
        cases = [case("c1", module="d", covers=["x"])]
        out = td.compute_summary([DOC_A], [req("r1", "doc-a", "supported", covers=["x"])], cases, module_scores, {})
        self.assertEqual([m["module"] for m in out["modules"]["critical"]], ["a", "b", "c"])
        self.assertFalse(out["document_rollup"][0]["touches_critical_code"])  # only touches "d", not in the top 3

    def test_a_story_touching_a_critical_module_is_flagged(self):
        module_scores = [{"module": "core", "pagerank": 0.9}, {"module": "misc", "pagerank": 0.1}]
        cases = [case("c1", module="core", covers=["x"])]
        requirements = [req("r1", "doc-a", "supported", covers=["x"])]
        out = td.compute_summary([DOC_A], requirements, cases, module_scores, {})
        self.assertTrue(out["document_rollup"][0]["touches_critical_code"])

    def test_critical_at_risk_excludes_verified_stories(self):
        module_scores = [{"module": "core", "pagerank": 0.9}]
        # doc-a is verified (fine); doc-b is broken and touches the same critical module.
        requirements = [req("r1", "doc-a", "supported", covers=["x"]), req("r2", "doc-b", "contradicted", covers=["y"])]
        cases = [case("c1", module="core", covers=["x"]), case("c2", module="core", covers=["y"])]
        out = td.compute_summary([DOC_A, DOC_B], requirements, cases, module_scores, {})
        self.assertEqual(out["documents"]["critical_at_risk"], 1)

    def test_overall_headline_names_critical_code_only_when_a_broken_story_touches_it(self):
        module_scores = [{"module": "core", "pagerank": 0.9}]
        requirements = [req("r1", "doc-a", "contradicted", covers=["x"])]
        cases = [case("c1", module="core", covers=["x"])]
        out = td.compute_summary([DOC_A], requirements, cases, module_scores, {})
        self.assertIn("your most critical code", out["overall"]["headline"])

    def test_overall_headline_stays_generic_when_the_broken_story_is_not_on_critical_code(self):
        # 4 modules ranked above "misc" so it's unambiguously outside the top 3.
        module_scores = [{"module": "a", "pagerank": 0.9}, {"module": "b", "pagerank": 0.8},
                          {"module": "c", "pagerank": 0.7}, {"module": "misc", "pagerank": 0.01}]
        requirements = [req("r1", "doc-a", "contradicted", covers=["x"])]
        cases = [case("c1", module="misc", covers=["x"])]
        out = td.compute_summary([DOC_A], requirements, cases, module_scores, {})
        self.assertNotIn("critical code", out["overall"]["headline"])


class ComputeSummaryOverallStatusTests(unittest.TestCase):
    """The dashboard's single headline verdict -- red/amber/green plus a
    one-sentence explanation, the first thing a product owner reads."""

    def test_any_broken_story_is_red_regardless_of_how_many_others_are_fine(self):
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-b", "contradicted")]
        out = td.compute_summary([DOC_A, DOC_B], requirements, [], [], {})
        self.assertEqual(out["overall"]["status"], "red")
        self.assertIn("1 is confirmed broken", out["overall"]["headline"])

    def test_every_story_verified_is_green(self):
        requirements = [req("r1", "doc-a", "supported")]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertEqual(out["overall"]["status"], "green")

    def test_in_progress_with_nothing_broken_is_amber(self):
        requirements = [req("r1", "doc-a", "unknown")]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertEqual(out["overall"]["status"], "amber")

    def test_no_documents_at_all_is_amber_not_an_error(self):
        out = td.compute_summary([], [], [], [], {})
        self.assertEqual(out["overall"]["status"], "amber")
        self.assertIn("No user stories yet", out["overall"]["headline"])


class ComputeSummaryCorrectnessRateTests(unittest.TestCase):
    """Of the criteria actually CODED (judged against real code -- supported or
    contradicted, never unknown), what fraction came back right. Distinct from
    contradiction_rate/supported_rate, which are shares of ALL criteria and so
    conflate "wrong" with "not built yet"."""

    def test_correctness_rate_excludes_uncoded_criteria_from_the_denominator(self):
        requirements = [req("r1", "doc-a", "supported"), req("r2", "doc-a", "contradicted"),
                         req("r3", "doc-a", "unknown"), req("r4", "doc-a", "unknown"), req("r5", "doc-a", "unknown")]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        # 1 of 2 CODED criteria (supported+contradicted) is correct -- not 1 of 5.
        self.assertEqual(out["requirements"]["correctness_rate"], 50.0)

    def test_correctness_rate_is_zero_not_an_error_when_nothing_is_coded_yet(self):
        requirements = [req("r1", "doc-a", "unknown")]
        out = td.compute_summary([DOC_A], requirements, [], [], {})
        self.assertEqual(out["requirements"]["correctness_rate"], 0.0)


class ComputeSummaryModuleCoverageTests(unittest.TestCase):
    def test_a_module_with_at_least_one_case_is_covered_one_without_is_a_gap(self):
        module_scores = [{"module": "inventory"}, {"module": "notifications"}]
        cases = [case("c1", module="inventory")]
        labels = {"notifications": {"name": "Notifications"}}
        out = td.compute_summary([], [], cases, module_scores, labels)
        self.assertEqual(out["modules"], {
            "total": 2, "with_coverage": 1, "without_coverage": 1, "coverage_pct": 50.0,
            # Only 2 modules total, so both fall inside the top-3-by-PageRank "critical" set.
            "critical": [{"module": "inventory", "display_name": "inventory"},
                         {"module": "notifications", "display_name": "Notifications"}],
        })
        self.assertEqual(out["gaps"]["modules_without_tests"], [{"module": "notifications", "display_name": "Notifications"}])

    def test_an_unlabeled_module_falls_back_to_its_raw_name(self):
        out = td.compute_summary([], [], [], [{"module": "code"}], {})
        self.assertEqual(out["gaps"]["modules_without_tests"], [{"module": "code", "display_name": "code"}])

    def test_no_successful_run_yet_zeroes_out_modules_without_erroring(self):
        out = td.compute_summary([DOC_A], [req("r1", "doc-a", "supported")], [], [], {})
        self.assertEqual(out["modules"], {"total": 0, "with_coverage": 0, "without_coverage": 0, "coverage_pct": 0.0, "critical": []})
        self.assertEqual(out["requirements"]["total"], 1)  # the rest of the dashboard still works


class ComputeSummaryTestCaseStatsTests(unittest.TestCase):
    def test_category_and_priority_are_counted_not_origin(self):
        cases = [
            case("c1", category="happy_path", priority=1),
            case("c2", category="happy_path", priority=2),
            case("c3", category="edge_case", priority=2),
        ]
        out = td.compute_summary([], [], cases, [], {})
        self.assertEqual(out["test_cases"], {
            "total": 3,
            "by_category": {"happy_path": 2, "edge_case": 1},
            "by_priority": {1: 1, 2: 2},
        })

    def test_a_case_missing_the_field_is_skipped_not_counted_as_a_bucket(self):
        cases = [case("c1", category="happy_path"), case("c2")]
        out = td.compute_summary([], [], cases, [], {})
        self.assertEqual(out["test_cases"]["by_category"], {"happy_path": 1})


class DashboardSnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
                          mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db")]
        for p in self._patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_upserting_the_same_day_twice_updates_in_place_not_duplicates(self):
        repo = db.create_repo({"name": "r2", "source_type": "local", "local_path": "/tmp"})
        db.upsert_dashboard_snapshot(repo["id"], "2026-10-25", {"requirements": {"total": 1}})
        db.upsert_dashboard_snapshot(repo["id"], "2026-10-25", {"requirements": {"total": 5}})
        rows = db.list_dashboard_snapshots(repo["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"]["requirements"]["total"], 5)

    def test_list_returns_oldest_first_chart_ready_order(self):
        repo = db.create_repo({"name": "r3", "source_type": "local", "local_path": "/tmp"})
        db.upsert_dashboard_snapshot(repo["id"], "2026-10-20", {"n": 1})
        db.upsert_dashboard_snapshot(repo["id"], "2026-10-22", {"n": 2})
        db.upsert_dashboard_snapshot(repo["id"], "2026-10-21", {"n": 3})
        rows = db.list_dashboard_snapshots(repo["id"])
        self.assertEqual([r["date"] for r in rows], ["2026-10-20", "2026-10-21", "2026-10-22"])

    def test_limit_keeps_only_the_most_recent_days(self):
        repo = db.create_repo({"name": "r4", "source_type": "local", "local_path": "/tmp"})
        for day in range(1, 6):
            db.upsert_dashboard_snapshot(repo["id"], f"2026-10-{day:02d}", {"n": day})
        rows = db.list_dashboard_snapshots(repo["id"], limit=2)
        self.assertEqual([r["summary"]["n"] for r in rows], [4, 5])

    def test_snapshots_are_scoped_to_their_own_repo(self):
        repo_a = db.create_repo({"name": "ra", "source_type": "local", "local_path": "/tmp"})
        repo_b = db.create_repo({"name": "rb", "source_type": "local", "local_path": "/tmp"})
        db.upsert_dashboard_snapshot(repo_a["id"], "2026-10-25", {"n": 1})
        db.upsert_dashboard_snapshot(repo_b["id"], "2026-10-25", {"n": 2})
        self.assertEqual(len(db.list_dashboard_snapshots(repo_a["id"])), 1)
        self.assertEqual(db.list_dashboard_snapshots(repo_a["id"])[0]["summary"]["n"], 1)


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
                          mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db")]
        for p in self._patches:
            p.start()
        db.init_db()
        self.client = TestClient(app)
        self.fixture_dir = tempfile.mkdtemp()
        (Path(self.fixture_dir) / "inventory.py").write_text(
            "class Inventory:\n    def reserve(self, sku, qty):\n        pass\n"
        )
        r = self.client.post("/api/repos", json={"name": "t", "source_type": "local", "local_path": self.fixture_dir})
        self.repo_id = r.json()["id"]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_404_for_an_unknown_repo(self):
        r = self.client.get("/api/repos/does-not-exist/dashboard")
        self.assertEqual(r.status_code, 404)

    def test_no_successful_run_yet_still_returns_200_with_modules_zeroed_out(self):
        r = self.client.get(f"/api/repos/{self.repo_id}/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["modules"], {"total": 0, "with_coverage": 0, "without_coverage": 0, "coverage_pct": 0.0, "critical": []})

    def test_real_seeded_data_rolls_up_correctly_and_a_contradiction_surfaces_as_a_gap(self):
        self.client.post(f"/api/repos/{self.repo_id}/run")
        doc = self.client.post(f"/api/repos/{self.repo_id}/documents", json={
            "name": "5111-inventory-reservation", "content": "Reserving stock must never go negative.",
        }).json()
        db.replace_story_requirements(self.repo_id, doc["id"], None, [{
            "text": "Reserving stock must never let it go negative.", "type": "positive",
            "status": "contradicted", "rationale": "reserve() never checks available stock",
            "covers": ["Inventory.reserve"],
        }])
        db.create_test_case(self.repo_id, "inventory", None, {
            "title": "Reserve more than available stock", "category": "error_handling",
            "steps": [{"action": "a", "expected": "e"}], "expected_result": "e", "covers": ["Inventory.reserve"],
        })

        r = self.client.get(f"/api/repos/{self.repo_id}/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["requirements"]["by_status"]["contradicted"], 1)
        self.assertEqual(len(body["gaps"]["contradicted_requirements"]), 1)
        self.assertEqual(body["gaps"]["contradicted_requirements"][0]["doc_name"], "5111-inventory-reservation")
        self.assertEqual(body["modules"], {
            "total": 1, "with_coverage": 1, "without_coverage": 0, "coverage_pct": 100.0,
            # The repo's only module is trivially its top-3-by-PageRank "critical" module.
            "critical": [{"module": "inventory", "display_name": "inventory"}],
        })
        self.assertTrue(body["document_rollup"][0]["has_test_coverage"])
        self.assertTrue(body["document_rollup"][0]["has_code"])
        self.assertEqual(body["documents"]["coded_pct"], 100.0)
        self.assertEqual(body["requirements"]["contradiction_rate"], 100.0)
        self.assertEqual(body["requirements"]["correctness_rate"], 0.0)  # the one coded criterion is wrong
        self.assertEqual(body["document_rollup"][0]["health"], "broken")
        self.assertEqual(body["documents"]["by_health"]["broken"], 1)
        self.assertNotIn("by_origin", body["test_cases"])  # deliberately not a PO-facing signal, see module docstring
        # The broken story's only module IS the repo's one critical module -- the
        # headline must say so, not stay generic.
        self.assertTrue(body["document_rollup"][0]["touches_critical_code"])
        self.assertEqual(body["documents"]["critical_at_risk"], 1)
        self.assertIn("your most critical code", body["overall"]["headline"])
        self.assertTrue(body["gaps"]["contradicted_requirements"][0]["touches_critical_code"])
        self.assertEqual(len(body["trend"]), 1)  # today's snapshot

    def test_calling_it_twice_the_same_day_writes_only_one_snapshot(self):
        self.client.get(f"/api/repos/{self.repo_id}/dashboard")
        r = self.client.get(f"/api/repos/{self.repo_id}/dashboard")
        self.assertEqual(len(r.json()["trend"]), 1)
        self.assertEqual(len(db.list_dashboard_snapshots(self.repo_id)), 1)


if __name__ == "__main__":
    unittest.main()
