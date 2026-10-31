"""Tests for server/test_case_import.py's parse_test_cases -- reconstructing
test cases from a flat, pipe-delimited-row text (the shape server/doc_extract.py's
CSV/Excel extraction produces), including a real round trip through
server/test_export.py's own CSV writer and server/doc_extract.py's own CSV reader.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from server import doc_extract, test_export
from server.test_case_import import parse_test_cases


def _header(*cols: str) -> str:
    return " | ".join(cols)


def _row(*cols) -> str:
    return " | ".join(str(c) for c in cols)


class ParseTestCasesTests(unittest.TestCase):
    def test_a_single_step_case_parses(self):
        text = "\n".join([
            _header("ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected",
                    "Priority", "Description", "Tags", "Area Path", "State"),
            _row("", "Test Case", "Add to cart", 1, "Click add to cart", "Item appears in cart",
                 2, "Verifies the add-to-cart flow", "Shopping Cart; Happy path; cart", "", "Design"),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertEqual(c["title"], "Add to cart")
        self.assertEqual(c["priority"], 2)
        self.assertEqual(c["category"], "happy_path")
        self.assertEqual(c["description"], "Verifies the add-to-cart flow")
        self.assertEqual(c["steps"], [{"action": "Click add to cart", "expected": "Item appears in cart"}])
        self.assertEqual(c["expected_result"], "Item appears in cart")

    def test_a_multi_step_case_groups_its_continuation_rows(self):
        text = "\n".join([
            _header("ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected",
                    "Priority", "Description", "Tags", "Area Path", "State"),
            _row("", "Test Case", "Checkout", 1, "Open cart", "Cart page shown",
                 1, "", "Checkout; Error handling; ", "", "Design"),
            _row("", "", "", 2, "Click checkout", "Payment form shown", "", "", "", "", ""),
            _row("", "", "", 3, "Submit payment", "Order confirmed", "", "", "", "", ""),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertEqual(c["title"], "Checkout")
        self.assertEqual(c["category"], "error_handling")
        self.assertEqual(len(c["steps"]), 3)
        self.assertEqual(c["steps"][1], {"action": "Click checkout", "expected": "Payment form shown"})
        self.assertEqual(c["expected_result"], "Order confirmed")

    def test_two_cases_in_one_file_are_kept_separate(self):
        text = "\n".join([
            _header("Title", "Step Action", "Step Expected", "Priority", "Tags"),
            _row("Case A", "Do A", "A happens", 1, "Edge case"),
            _row("Case B", "Do B", "B happens", 3, "Happy path"),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual([c["title"] for c in cases], ["Case A", "Case B"])
        self.assertEqual(cases[0]["category"], "edge_case")
        self.assertEqual(cases[1]["category"], "happy_path")

    def test_an_unrecognized_category_label_falls_back_to_unclassified(self):
        text = "\n".join([
            _header("Title", "Step Action", "Step Expected", "Tags"),
            _row("Legacy case", "Do the thing", "It works", "Smoke; P1"),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual(cases[0]["category"], "unclassified")

    def test_a_non_numeric_priority_becomes_none(self):
        text = "\n".join([
            _header("Title", "Step Action", "Step Expected", "Priority"),
            _row("Case", "Do", "Done", "High"),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(cases[0]["priority"])

    def test_missing_required_columns_returns_a_clear_error_not_a_guess(self):
        text = "\n".join([
            _header("Summary", "Owner"),
            _row("Not a test case export", "someone"),
        ])
        cases, error = parse_test_cases(text)
        self.assertEqual(cases, [])
        self.assertIsNotNone(error)
        self.assertIn("Title", error)

    def test_empty_text_returns_an_error_not_a_crash(self):
        cases, error = parse_test_cases("")
        self.assertEqual(cases, [])
        self.assertIsNotNone(error)

    def test_a_blank_row_is_skipped(self):
        text = "\n".join([
            _header("Title", "Step Action", "Step Expected"),
            _row("Real case", "Do", "Done"),
            " |  | ",
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual(len(cases), 1)

    def test_round_trip_through_the_real_xlsx_export_and_extraction(self):
        """Regression: an .xlsx upload was rejected with "no recognizable
        Title column" on every single file, even a well-formed one --
        server/doc_extract.py's XLSX extraction inserts a "--- Sheet: X ---"
        marker line ahead of the real header row, which this module was
        mistaking FOR the header. Found by manual QA on 2026-10-31."""
        text = "\n".join([
            "--- Sheet: Sheet1 ---",
            _header("ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected",
                    "Priority", "Description", "Tags", "Area Path", "State"),
            _row("", "Test Case", "Reserve inventory", 1, "Request 2 units", "2 units reserved",
                 1, "Checks reservation", "inventory; Happy path; reserve", "", "Design"),
        ])
        cases, error = parse_test_cases(text)
        self.assertIsNone(error)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["title"], "Reserve inventory")
        self.assertEqual(cases[0]["category"], "happy_path")

    def test_round_trip_through_the_real_csv_export_and_extraction(self):
        """server/test_export.to_csv -> server/doc_extract.extract_text ->
        this module -- the exact path TestAtlas's own round-tripped export
        takes through the Import test cases button."""
        source_cases = [{
            "title": "Reserve inventory", "category": "happy_path", "priority": 1,
            "description": "Checks stock is reserved correctly",
            "steps": [
                {"action": "Request 2 units", "expected": "2 units reserved"},
                {"action": "Request 1 more unit", "expected": "3 units reserved total"},
            ],
            "module": "inventory",
        }]
        csv_text = test_export.to_csv(source_cases, labels={})
        extracted = doc_extract.extract_text("export.csv", csv_text.encode("utf-8"))
        cases, error = parse_test_cases(extracted)
        self.assertIsNone(error)
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertEqual(c["title"], "Reserve inventory")
        self.assertEqual(c["priority"], 1)
        self.assertEqual(c["category"], "happy_path")
        self.assertEqual(len(c["steps"]), 2)
        self.assertEqual(c["steps"][0], {"action": "Request 2 units", "expected": "2 units reserved"})
        self.assertEqual(c["steps"][1], {"action": "Request 1 more unit", "expected": "3 units reserved total"})
        self.assertEqual(c["expected_result"], "3 units reserved total")


if __name__ == "__main__":
    unittest.main()
