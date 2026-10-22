"""Tests for server/work_item_import.py's split_work_items -- splitting a
flat, pipe-delimited-row text (the shape server/doc_extract.py's CSV/Excel
extraction produces) into one document per recognized work-item row, with a
documented single-document fallback when no header is recognized.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from server.work_item_import import split_work_items


class SplitWorkItemsTests(unittest.TestCase):
    def test_a_realistic_ado_export_splits_one_document_per_row(self):
        text = "\n".join([
            "ID | Work Item Type | Title | Description | Acceptance Criteria",
            "101 | User Story | Add to cart | Shoppers add items to their cart | Given an item, when added, then it appears in the cart",
            "102 | User Story | Checkout | Shoppers pay for their order | Given a non-empty cart, when checkout runs, then payment is captured",
        ])
        items = split_work_items(text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["name"], "101: Add to cart")
        self.assertIn("Shoppers add items to their cart", items[0]["content"])
        self.assertIn("Given an item, when added, then it appears in the cart", items[0]["content"])
        self.assertEqual(items[1]["name"], "102: Checkout")

    def test_no_recognizable_header_falls_back_to_one_whole_file_document(self):
        text = "Just some prose.\nNo header row here at all, nothing to split on."
        items = split_work_items(text)
        self.assertEqual(items, [{"name": None, "content": text}])

    def test_a_header_missing_description_still_works(self):
        text = "\n".join([
            "ID | Title | Acceptance Criteria",
            "7 | Apply discount | Given a valid code, when applied, then the total drops",
        ])
        items = split_work_items(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "7: Apply discount")
        self.assertIn("Given a valid code, when applied, then the total drops", items[0]["content"])

    def test_a_blank_row_is_skipped_not_turned_into_an_empty_document(self):
        text = "\n".join([
            "ID | Title | Description",
            "1 | Real story | has content",
            " |  | ",
            "2 | Another real story | also has content",
        ])
        items = split_work_items(text)
        self.assertEqual(len(items), 2)
        self.assertEqual([i["name"] for i in items], ["1: Real story", "2: Another real story"])

    def test_only_title_matching_is_not_enough_to_recognize_a_header(self):
        # "title" alone is one recognized column -- below _MIN_RECOGNIZED_COLUMNS,
        # so this must NOT be mistaken for a real header and split on.
        text = "The title of this document is a love letter to my cart.\nIt has no real structure."
        items = split_work_items(text)
        self.assertEqual(items, [{"name": None, "content": text}])

    def test_empty_text_returns_no_items(self):
        self.assertEqual(split_work_items(""), [])

    def test_a_row_with_no_id_or_title_falls_back_to_untitled(self):
        text = "\n".join([
            "ID | Title | Description",
            " | ".join(["", "", "Only a description, no id or title"]),
        ])
        items = split_work_items(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Untitled work item")


if __name__ == "__main__":
    unittest.main()
