"""Tests for server/doc_extract.py -- plain-text extraction from uploaded
documents. Focused on the CSV/XLSX -> "cell | cell | cell" conversion: it has
to preserve each row's column POSITIONS, not just its non-blank content,
because server/work_item_import.py (tested separately in
tests/test_work_item_import.py) looks a row's fields up by the header's
column index. A blank cell silently dropped from the middle of a row shifts
every column after it out of position for that row -- on a real ADO export
this is routine (many stories skip the optional Description field), so it
isn't a corner case, it's the common one.
Run with:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import io
import unittest

from server import doc_extract
from server.work_item_import import split_work_items


def _csv_bytes(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


class ExtractCsvTests(unittest.TestCase):
    def test_a_blank_interior_cell_keeps_every_later_column_in_position(self):
        # Row 2's Description is blank -- a real, routine ADO export shape, not a
        # malformed file. Acceptance Criteria must still land under its own column,
        # not get shifted left into Description's empty slot.
        data = _csv_bytes(
            "ID,Title,Description,Acceptance Criteria\n"
            '5111,Inventory reservation,Reserve stock for an order,"Given stock is available, when reserved, then it is deducted"\n'
            '5112,Account notifications,,"Given a signup, when complete, then a welcome email is sent"\n'
        )
        text = doc_extract.extract_text("export.csv", data)
        lines = text.splitlines()
        self.assertEqual(lines[0], "ID | Title | Description | Acceptance Criteria")
        self.assertEqual(
            lines[2],
            "5112 | Account notifications |  | Given a signup, when complete, then a welcome email is sent",
        )

    def test_a_blank_id_column_keeps_later_columns_in_position_too(self):
        data = _csv_bytes(
            "ID,Title,Description,Acceptance Criteria\n"
            ',Inventory reservation,,"Given stock is available, when reserved, then it is deducted"\n'
        )
        text = doc_extract.extract_text("export.csv", data)
        self.assertEqual(
            text.splitlines()[1],
            " | Inventory reservation |  | Given stock is available, when reserved, then it is deducted",
        )

    def test_a_fully_blank_row_is_still_skipped(self):
        data = _csv_bytes("ID,Title\n1,Real story\n,\n2,Another real story\n")
        text = doc_extract.extract_text("export.csv", data)
        self.assertEqual(text.splitlines(), ["ID | Title", "1 | Real story", "2 | Another real story"])

    def test_a_quoted_field_with_a_comma_and_embedded_newline_is_handled_correctly(self):
        data = _csv_bytes(
            'ID,Title,Acceptance Criteria\n'
            '1,Checkout,"Given a cart with items, a tax, and a discount,\nwhen checkout runs, then the total is correct"\n'
        )
        text = doc_extract.extract_text("export.csv", data)
        self.assertEqual(
            text.splitlines()[1],
            "1 | Checkout | Given a cart with items, a tax, and a discount, when checkout runs, then the total is correct",
        )

    def test_an_excel_added_bom_is_stripped(self):
        data = b"\xef\xbb\xbf" + _csv_bytes("ID,Title\n1,Story")
        text = doc_extract.extract_text("export.csv", data)
        self.assertEqual(text.splitlines()[0], "ID | Title")

    def test_a_multi_line_bulleted_acceptance_criteria_field_never_fragments_its_own_row(self):
        # The more severe symptom of the same embedded-newline bug: ADO's Acceptance
        # Criteria is routinely a multi-line bulleted list once exported to CSV. Before
        # the fix, each embedded newline split what should be ONE row's text across
        # several lines of the pipe-delimited output -- and server/work_item_import.py
        # reads that output one LINE per row, so the row's own later fields (here, a
        # second story that comes right after it) silently got corrupted or truncated.
        data = _csv_bytes(
            "ID,Title,Acceptance Criteria\n"
            '1,Checkout,"Given a cart with items\nwhen checkout runs\nthen the total is correct"\n'
            "2,Login,\"Given valid credentials, when login runs, then access is granted\"\n"
        )
        text = doc_extract.extract_text("export.csv", data)
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)  # header + exactly 2 data rows, never more
        self.assertEqual(
            lines[1],
            "1 | Checkout | Given a cart with items when checkout runs then the total is correct",
        )
        self.assertEqual(
            lines[2],
            "2 | Login | Given valid credentials, when login runs, then access is granted",
        )


class ExtractXlsxTests(unittest.TestCase):
    def _xlsx_bytes(self, rows: list[list]) -> bytes:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_a_blank_interior_cell_keeps_every_later_column_in_position(self):
        data = self._xlsx_bytes([
            ["ID", "Title", "Description", "Acceptance Criteria"],
            [5111, "Inventory reservation", "Reserve stock for an order", "Given stock is available, when reserved, then it is deducted"],
            [5112, "Account notifications", None, "Given a signup, when complete, then a welcome email is sent"],
        ])
        text = doc_extract.extract_text("export.xlsx", data)
        lines = text.splitlines()
        self.assertIn(
            "5112 | Account notifications |  | Given a signup, when complete, then a welcome email is sent",
            lines,
        )

    def test_a_fully_blank_row_is_still_skipped(self):
        data = self._xlsx_bytes([["ID", "Title"], [1, "Real story"], [None, None], [2, "Another real story"]])
        text = doc_extract.extract_text("export.xlsx", data)
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("---")]
        self.assertEqual(lines, ["ID | Title", "1 | Real story", "2 | Another real story"])


class UnsupportedAndEmptyTests(unittest.TestCase):
    def test_an_unsupported_extension_is_rejected_with_a_clear_message(self):
        with self.assertRaises(doc_extract.UnsupportedDocumentError) as ctx:
            doc_extract.extract_text("notes.exe", b"whatever")
        self.assertIn("Unsupported file type", str(ctx.exception))

    def test_a_csv_with_nothing_in_it_is_rejected_not_silently_empty(self):
        with self.assertRaises(doc_extract.UnsupportedDocumentError) as ctx:
            doc_extract.extract_text("export.csv", b"\n\n\n")
        self.assertIn("Couldn't find any text", str(ctx.exception))


class EndToEndAdoImportTests(unittest.TestCase):
    """The scenario this whole fix is about: a real multi-row ADO CSV export
    with the usual mix of filled and blank optional fields, run through the
    FULL real pipeline (extract_text -> split_work_items) exactly as
    server/app.py's /documents/import-work-items route does."""

    def test_twenty_rows_with_some_blank_descriptions_each_become_their_own_correct_document(self):
        header = "ID,Title,Description,Acceptance Criteria"
        rows = []
        for i in range(1, 21):
            desc = "" if i % 3 == 0 else f"Description for story {i}"  # every 3rd row skips Description, like a real export
            rows.append(f'{5100 + i},Story {i},{desc},"Given condition {i}, when it happens, then outcome {i}"')
        data = _csv_bytes(header + "\n" + "\n".join(rows) + "\n")

        text = doc_extract.extract_text("export.csv", data)
        items = split_work_items(text)

        self.assertEqual(len(items), 20)  # every row became its own document
        for i, item in enumerate(items, 1):
            self.assertEqual(item["name"], f"{5100 + i}: Story {i}")
            self.assertIn(f"Given condition {i}, when it happens, then outcome {i}", item["content"])
            if i % 3 == 0:
                self.assertNotIn("Description for story", item["content"])  # correctly blank, not misattributed
            else:
                self.assertIn(f"Description for story {i}", item["content"])


if __name__ == "__main__":
    unittest.main()
