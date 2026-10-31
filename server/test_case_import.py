"""Parses an already-extracted, pipe-delimited-row text (server/doc_extract.py --
the SAME shape whether the upload was a real .csv or an .xlsx) into test cases,
for bringing an already-existing suite into TestAtlas (e.g. a QA team's real
Azure DevOps Test Plans export, or TestAtlas's own CSV export round-tripped
back in). See server/app.py's /test-cases/import route.

Mirrors server/work_item_import.py's header-recognition style exactly (tolerant,
case-insensitive substring match against server/test_export.py's own CSV_COLUMNS
names), but unlike that module, a file that doesn't match the expected shape has
no sensible single-blob fallback here -- a test case fundamentally needs a title
and at least one step to mean anything, so an unrecognized file returns an empty
list with a clear reason instead of guessing.

Row grouping mirrors server/test_export.py's own to_csv() write logic exactly:
only the FIRST row of a case has a non-blank Title; every further row in that
same case repeats only the step columns (Test Step / Step Action / Step
Expected), with everything else blank -- so "a new case starts whenever Title
is non-blank" is the same invariant the exporter wrote, not a guess.
"""
from __future__ import annotations

_COLUMN_PATTERNS: dict[str, list[str]] = {
    "title": ["title"],
    "action": ["step action", "action"],
    "expected": ["step expected", "expected result", "expected"],
    "priority": ["priority"],
    "description": ["description"],
    "tags": ["tags"],
}

# A test case needs at least a title and a way to tell what each step does and
# expects -- without these three, there's nothing to safely reconstruct.
_REQUIRED_FIELDS = ("title", "action", "expected")

# Reverses server/test_export.py's _CATEGORY_LABELS (module name; CATEGORY LABEL;
# feature, "; "-joined into Tags) so TestAtlas's own round-tripped export comes
# back as the same enum value it left as -- matched case-insensitively since
# nothing guarantees a hand-edited file kept the exact casing.
_CATEGORY_LABELS_REVERSED = {"happy path": "happy_path", "edge case": "edge_case", "error handling": "error_handling"}

# A genuinely external CSV (hand-authored by a QA team, never exported by
# TestAtlas) won't have a Tags cell shaped like TestAtlas's own export at all --
# rather than force a guess, an unrecognized category lands here. Safe: the
# frontend's category-label helper (tdCategoryLabel, static/app.js) already
# falls back to showing an unrecognized key verbatim instead of breaking.
_UNCLASSIFIED_CATEGORY = "unclassified"


def _match_header(header_cells: list[str]) -> dict[str, int] | None:
    """{field: column index} for whichever _COLUMN_PATTERNS fields this header
    recognizes, or None if the required fields (_REQUIRED_FIELDS) aren't all
    present."""
    found: dict[str, int] = {}
    for i, cell in enumerate(header_cells):
        low = cell.lower().strip()
        for field, patterns in _COLUMN_PATTERNS.items():
            if field in found:
                continue
            if any(p in low for p in patterns):
                found[field] = i
                break
    return found if all(f in found for f in _REQUIRED_FIELDS) else None


def _category_from_tags(tags: str) -> str:
    for segment in tags.split(";"):
        mapped = _CATEGORY_LABELS_REVERSED.get(segment.strip().lower())
        if mapped:
            return mapped
    return _UNCLASSIFIED_CATEGORY


def parse_test_cases(text: str) -> tuple[list[dict], str | None]:
    """Returns (cases, None) on success, or ([], "a clear reason") when the
    file doesn't match the expected shape or contains no usable rows -- never
    raises. Each case dict: {title, category, priority, description, steps:
    [{action, expected}, ...], expected_result}, ready for db.import_test_cases."""
    # server/doc_extract.py's XLSX extraction inserts a "--- Sheet: X ---"
    # marker line ahead of each sheet's own rows -- real structure, not a row
    # of data. Left in, it gets mistaken for the header line itself (breaking
    # header recognition on EVERY .xlsx upload, not just a multi-sheet one).
    # Drop every such marker before header detection or row grouping sees it.
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("---")]
    if not lines:
        return [], "That file doesn't have any content to import."

    header_cells = [c.strip() for c in lines[0].split(" | ")]
    columns = _match_header(header_cells)
    if not columns:
        return [], (
            "Couldn't find a recognizable Title column and step action/expected-result "
            "columns in this file's header row -- expected the same shape TestAtlas's own "
            "CSV export uses (or a similar Azure DevOps Test Plans export)."
        )

    def cell(cells: list[str], field: str) -> str:
        idx = columns.get(field)
        return cells[idx] if idx is not None and idx < len(cells) else ""

    groups: list[list[list[str]]] = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(" | ")]
        if not any(cells):
            continue
        if cell(cells, "title") or not groups:
            groups.append([cells])
        else:
            groups[-1].append(cells)

    cases: list[dict] = []
    for group in groups:
        first = group[0]
        title = cell(first, "title")
        if not title:
            continue  # malformed: a case's first row with no title -- nothing to build from
        steps = [{"action": cell(c, "action"), "expected": cell(c, "expected")} for c in group]
        priority_raw = cell(first, "priority").strip()
        priority = int(priority_raw) if priority_raw.isdigit() else None
        cases.append({
            "title": title,
            "category": _category_from_tags(cell(first, "tags")),
            "priority": priority,
            "description": cell(first, "description"),
            "steps": steps,
            "expected_result": steps[-1]["expected"] if steps else "",
        })

    if not cases:
        return [], "No test cases were found in this file -- every row was blank or missing a title."
    return cases, None
