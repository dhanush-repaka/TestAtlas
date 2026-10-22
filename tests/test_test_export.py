"""Test-case export (server/test_export.py). Run with:  python -m unittest discover -s tests -t ."""
from __future__ import annotations

import csv
import io
import json
import unittest

from server import test_export as ex

LABELS = {"components.cart": {"name": "Shopping Cart", "kind": "feature", "description": "Add and remove items."}}
DOCS = {"doc-1": "5111-inventory-reservation"}
JOURNEYS = {"journey-1": "Checkout flow"}
CASE = {
    "module": "components.cart", "title": "Verify that a shopper can add a product", "description": "Core purchase flow.",
    "priority": 1, "category": "happy_path", "feature": "Add to cart", "preconditions": "A product page is open",
    "steps": [{"action": 'Click "Add To Cart"', "expected": "The cart shows 1 item"},
              {"action": "Open the cart", "expected": "The product is listed"},
              {"action": "Read the total", "expected": "It matches the price"}],
    "covers": ["AddToCart"],
}
LEGACY = {"module": "components.cart", "title": "Old unit-style case", "category": "edge_case",
          "steps": ["Call add with 0", "Check result"], "expected_result": "Nothing is added"}


def rows(text):
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


class CsvTests(unittest.TestCase):
    def test_ado_import_shape_one_row_per_step_with_the_case_on_the_first(self):
        r = rows(ex.to_csv([CASE], LABELS))
        self.assertEqual(r[0], ex.CSV_COLUMNS)
        head, s2, s3 = r[1:]
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        self.assertEqual((head[col["Work Item Type"]], head[col["Title"]], head[col["Test Step"]]), ("Test Case", CASE["title"], "1"))
        self.assertEqual((head[col["Step Action"]], head[col["Step Expected"]]), ('Click "Add To Cart"', "The cart shows 1 item"))
        self.assertEqual(head[col["Priority"]], "1")
        self.assertIn("Preconditions: A product page is open", head[col["Description"]])
        self.assertIn("Code traceability: module components.cart; covers AddToCart", head[col["Description"]])
        self.assertEqual(head[col["Tags"]], "Shopping Cart; Happy path; Add to cart")     # plain-English module name, not the dotted one
        for step, n in ((s2, "2"), (s3, "3")):                                            # later steps carry no case fields
            self.assertEqual((step[col["Work Item Type"]], step[col["Title"]], step[col["Test Step"]]), ("", "", n))

    def test_traceability_lists_module_and_covered_code_and_degrades_gracefully(self):
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        many = {**CASE, "covers": ["AddToCart", "CartService.addItem", "CartModal"]}
        self.assertIn("covers AddToCart, CartService.addItem, CartModal", rows(ex.to_csv([many], LABELS))[1][col["Description"]])
        none = {**CASE, "covers": []}
        d = rows(ex.to_csv([none], LABELS))[1][col["Description"]]
        self.assertIn("Code traceability: module components.cart", d)                    # module still traced
        bare = {"title": "t", "category": "happy_path", "steps": [{"action": "a", "expected": "e"}]}
        # no module, no covers, no description, no preconditions -- but the two traceability
        # lines are never silently dropped, each says explicitly that it found nothing
        d = rows(ex.to_csv([bare], LABELS))[1][col["Description"]]
        self.assertIn("Code traceability: not linked to any code module", d)
        self.assertIn("User story traceability: not linked to any user story", d)

    def test_a_case_with_no_user_story_link_at_all_says_so_explicitly(self):
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        d = rows(ex.to_csv([CASE], LABELS, DOCS, JOURNEYS))[1][col["Description"]]
        self.assertIn("User story traceability: not linked to any user story", d)

    def test_a_case_generated_directly_from_a_document_names_it(self):
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        story_case = {**CASE, "source_doc_id": "doc-1"}
        d = rows(ex.to_csv([story_case], LABELS, DOCS, JOURNEYS))[1][col["Description"]]
        self.assertIn('User story traceability: "5111-inventory-reservation"', d)

    def test_a_case_generated_from_a_journey_names_it(self):
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        journey_case = {**CASE, "source_journey_id": "journey-1"}
        d = rows(ex.to_csv([journey_case], LABELS, DOCS, JOURNEYS))[1][col["Description"]]
        self.assertIn('User story traceability: "Checkout flow"', d)

    def test_a_code_driven_case_surfaces_a_user_story_it_only_shares_a_citation_with(self):
        # This is the case the user flagged: a module-level case has no source_doc_id of
        # its own, but Story Coverage's own heuristic (related_requirements, computed by
        # llm_story_coverage.link_requirements_to_cases before export) ties it to a
        # document through a shared code citation -- that must still show up here.
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        linked = {**CASE, "related_requirements": [{"doc_id": "doc-1", "text": "x", "status": "supported"}]}
        d = rows(ex.to_csv([linked], LABELS, DOCS, JOURNEYS))[1][col["Description"]]
        self.assertIn('User story traceability: "5111-inventory-reservation"', d)

    def test_a_doc_id_with_no_matching_name_degrades_gracefully_instead_of_erroring(self):
        col = {n: i for i, n in enumerate(ex.CSV_COLUMNS)}
        story_case = {**CASE, "source_doc_id": "doc-unknown"}
        d = rows(ex.to_csv([story_case], LABELS, DOCS, JOURNEYS))[1][col["Description"]]
        self.assertIn('User story traceability: "an unknown document"', d)

    def test_bom_for_excel_and_commas_quotes_newlines_survive(self):
        tricky = {**CASE, "title": 'Verify "quoted", commas', "steps": [{"action": "Line one\nline two", "expected": "a, b"}]}
        text = ex.to_csv([tricky], LABELS)
        self.assertTrue(text.startswith("﻿"))
        self.assertEqual(rows(text)[1][2], 'Verify "quoted", commas')

    def test_formula_injection_is_neutralised(self):
        evil = {**CASE, "title": "=HYPERLINK(\"http://x\")", "steps": [{"action": "+cmd", "expected": "@SUM(A1)"}]}
        r = rows(ex.to_csv([evil], LABELS))[1]
        self.assertTrue(r[2].startswith("'=")); self.assertTrue(r[4].startswith("'+")); self.assertTrue(r[5].startswith("'@"))

    def test_legacy_string_steps_are_exported_with_the_overall_result_on_the_last(self):
        r = rows(ex.to_csv([LEGACY], LABELS))
        self.assertEqual([x[5] for x in r[1:]], ["", "Nothing is added"])
        self.assertEqual(r[1][4], "Call add with 0")


class MarkdownAndJsonTests(unittest.TestCase):
    def test_markdown_groups_by_module_name_with_a_steps_table(self):
        md = ex.to_markdown([CASE, LEGACY], LABELS, "commerce", DOCS, JOURNEYS)
        for needle in ("# Test cases: commerce", "## Shopping Cart", "_Add and remove items._", "### Verify that a shopper can add a product",
                       "**Preconditions:** A product page is open", "_Code traceability: module components.cart; covers AddToCart_",
                       "_User story traceability: not linked to any user story_",
                       "| 1 | Click \"Add To Cart\" | The cart shows 1 item |", "Priority 1 · Happy path · Add to cart"):
            self.assertIn(needle, md)
        self.assertEqual(md.count("## Shopping Cart"), 1)

    def test_markdown_names_the_linked_user_story(self):
        md = ex.to_markdown([{**CASE, "source_doc_id": "doc-1"}], LABELS, "commerce", DOCS, JOURNEYS)
        self.assertIn('_User story traceability: "5111-inventory-reservation"_', md)

    def test_markdown_escapes_pipes_so_tables_dont_break(self):
        md = ex.to_markdown([{**CASE, "steps": [{"action": "a | b", "expected": "c"}]}], LABELS, "r")
        self.assertIn("a \\| b", md)

    def test_json_carries_names_steps_and_covers(self):
        out = json.loads(ex.to_json([CASE], LABELS))["test_cases"][0]
        self.assertEqual((out["module_name"], len(out["steps"]), out["covers"]), ("Shopping Cart", 3, ["AddToCart"]))

    def test_json_code_traceability_reflects_presence_and_absence(self):
        out = json.loads(ex.to_json([CASE], LABELS))["test_cases"][0]
        self.assertEqual(out["code_traceability"], {"linked": True, "module": "components.cart", "covers": ["AddToCart"]})
        bare = {"title": "t", "category": "happy_path", "steps": [{"action": "a", "expected": "e"}]}
        out = json.loads(ex.to_json([bare], LABELS))["test_cases"][0]
        self.assertEqual(out["code_traceability"], {"linked": False, "module": None, "covers": []})

    def test_json_user_story_traceability_reflects_presence_and_absence(self):
        out = json.loads(ex.to_json([CASE], LABELS, DOCS, JOURNEYS))["test_cases"][0]
        self.assertEqual(out["user_story_traceability"], {"linked": False, "stories": []})
        story_case = {**CASE, "source_doc_id": "doc-1"}
        out = json.loads(ex.to_json([story_case], LABELS, DOCS, JOURNEYS))["test_cases"][0]
        self.assertEqual(out["user_story_traceability"],
                         {"linked": True, "stories": [{"kind": "document", "id": "doc-1", "name": "5111-inventory-reservation"}]})

    def test_json_journey_case_is_distinguished_from_a_document_case(self):
        journey_case = {**CASE, "source_journey_id": "journey-1"}
        out = json.loads(ex.to_json([journey_case], LABELS, DOCS, JOURNEYS))["test_cases"][0]
        self.assertEqual(out["user_story_traceability"]["stories"], [{"kind": "journey", "id": "journey-1", "name": "Checkout flow"}])

    def test_filename_is_a_safe_slug(self):
        self.assertEqual(ex.filename("commerce (Next.js storefront)", "csv"), "commerce-next-js-storefront-test-cases.csv")
        self.assertEqual(ex.filename("///", "md"), "repo-test-cases.md")


if __name__ == "__main__":
    unittest.main()
