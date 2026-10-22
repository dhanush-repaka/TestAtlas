"""Tests for entry-point detection (kg/python_ast_parser.py's route-decorator
reading) and journey detection/clustering (kg/journeys.py) -- both
deterministic, no LLM involved. Also covers the stable-id/upsert discipline
replace_journeys relies on, so a fresh detection pass never orphans a
story_requirements.journey_id reference.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from kg import journeys as kj
from kg import repo_parser
from kg.dev_graph_builder import build_dev_graph

CHECKOUT_ROUTES = '''
from fastapi import APIRouter
from services import process_payment, lookup_order, run_search
router = APIRouter()

@router.post("/checkout/pay")
def pay(cart):
    return process_payment(cart)

@router.get("/checkout/confirm")
def confirm(order_id):
    return lookup_order(order_id)

@router.get("/search")
def search(query):
    return run_search(query)

def not_a_route():
    return 1
'''

CHECKOUT_SERVICES = '''
def process_payment(cart):
    return charge_card(cart)

def charge_card(cart):
    return True

def lookup_order(order_id):
    return {"id": order_id}

def run_search(query):
    return []
'''


class EntryPointDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "routes.py").write_text(CHECKOUT_ROUTES)
        (self.tmp / "services.py").write_text(CHECKOUT_SERVICES)
        self.g = build_dev_graph(repo_parser.parse_repo(self.tmp))

    def test_route_decorated_functions_are_flagged_entry_points(self):
        entries = {n: d for n, d in self.g.nodes(data=True) if d.get("is_entry_point")}
        labels = {d["label"] for d in entries.values()}
        self.assertEqual(labels, {"pay", "confirm", "search"})

    def test_route_method_and_path_are_captured(self):
        pay = next(d for n, d in self.g.nodes(data=True) if d.get("label") == "pay")
        self.assertEqual(pay["routes"], [{"method": "post", "path": "/checkout/pay"}])

    def test_ordinary_function_is_not_an_entry_point(self):
        plain = next(d for n, d in self.g.nodes(data=True) if d.get("label") == "not_a_route")
        self.assertNotIn("is_entry_point", plain)


class DetectJourneysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "routes.py").write_text(CHECKOUT_ROUTES)
        (self.tmp / "services.py").write_text(CHECKOUT_SERVICES)
        self.g = build_dev_graph(repo_parser.parse_repo(self.tmp))

    def test_clusters_by_shared_route_prefix(self):
        found = kj.detect_journeys(self.g)
        prefixes = {j["prefix"] for j in found}
        self.assertEqual(prefixes, {"/checkout", "/search"})

    def test_checkout_journey_reaches_both_its_entry_points_and_their_calls(self):
        found = kj.detect_journeys(self.g)
        checkout = next(j for j in found if j["prefix"] == "/checkout")
        self.assertEqual(len(checkout["entry_point_ids"]), 2)  # pay + confirm
        labels = {self.g.nodes[n]["label"] for n in checkout["node_ids"] if n in self.g}
        self.assertEqual(labels, {"pay", "process_payment", "charge_card", "confirm", "lookup_order"})

    def test_search_journey_is_disjoint_from_checkout(self):
        found = kj.detect_journeys(self.g)
        checkout = next(j for j in found if j["prefix"] == "/checkout")
        search = next(j for j in found if j["prefix"] == "/search")
        self.assertFalse(set(checkout["node_ids"]) & set(search["node_ids"]))

    def test_no_entry_points_returns_empty(self):
        tmp2 = Path(tempfile.mkdtemp())
        (tmp2 / "plain.py").write_text("def f():\n    return 1\n")
        g2 = build_dev_graph(repo_parser.parse_repo(tmp2))
        self.assertEqual(kj.detect_journeys(g2), [])

    def test_journey_ids_are_stable_across_repeated_detection(self):
        first = {j["id"] for j in kj.detect_journeys(self.g)}
        second = {j["id"] for j in kj.detect_journeys(self.g)}
        self.assertEqual(first, second)
        self.assertTrue(all(i == f"journey:{i.split(':', 1)[1]}" for i in first))


class ReplaceJourneysUpsertTests(unittest.TestCase):
    """A fresh detection pass must UPSERT by stable id, never delete-then-
    reinsert -- otherwise a story_requirements.journey_id foreign key
    (especially a human-confirmed answer) would be orphaned by re-running
    analysis and re-detecting journeys."""

    def setUp(self):
        from server import db
        self.db = db
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            unittest.mock.patch.object(db, "DATA_DIR", Path(self._tmp.name)),
            unittest.mock.patch.object(db, "DB_PATH", Path(self._tmp.name) / "kg.db"),
        ]
        for p in self._patches:
            p.start()
        db.init_db()
        self.repo = db.create_repo({"name": "t", "source_type": "local", "local_path": "/tmp"})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_rerunning_detection_preserves_an_existing_journeys_row_and_its_references(self):
        journey = {"id": "journey:checkout", "prefix": "/checkout", "name": "Checkout",
                   "entry_point_ids": ["function:routes:pay"], "node_ids": ["function:routes:pay", "function:services:charge_card"]}
        self.db.replace_journeys(self.repo["id"], "run-1", [journey])

        # A human-confirmed requirement pointing at this journey.
        self.db.replace_derived_requirements(self.repo["id"], "journey:checkout", "run-1", [
            {"text": "x", "type": "positive", "status": "needs_human_input", "covers": ["pay"], "question": "ok?"},
        ])
        req_id = self.db.list_story_requirements(self.repo["id"])[0]["id"]
        self.db.confirm_story_requirement(req_id, "supported")

        # Re-run detection with the SAME journey (as kg.journeys.detect_journeys would
        # produce again from a fresh graph with the same routes) -- an upsert, not a wipe.
        self.db.replace_journeys(self.repo["id"], "run-2", [{**journey, "name": "Checkout Flow"}])

        journeys = self.db.list_journeys(self.repo["id"])
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0]["id"], "journey:checkout")
        self.assertEqual(journeys[0]["name"], "Checkout Flow")

        confirmed = self.db.get_story_requirement(req_id)
        self.assertEqual(confirmed["source"], "human_confirmed")
        self.assertEqual(confirmed["status"], "supported")
        self.assertEqual(confirmed["journey_id"], "journey:checkout")


if __name__ == "__main__":
    unittest.main()
