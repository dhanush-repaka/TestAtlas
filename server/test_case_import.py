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


def parse_rows(sheets):
    """Parse structured sheets without flattening or losing cell boundaries."""
    cases=[]
    for sheet_name,rows in sheets:
        columns=None
        pending=None
        def flush():
            if pending is None:
                return
            if not pending['steps']:
                raise ValueError(f"{sheet_name}: case {pending['title']!r} has no usable steps")
            pending['expected_result']=pending['steps'][-1]['expected']
            cases.append(pending.copy())
        for number,row in enumerate(rows,1):
            cells=[str(value).strip() if value is not None else '' for value in row]
            if not any(cells):
                continue
            header=_match_header(cells)
            if header and columns is not None and not all(cells[index].lower() in _COLUMN_PATTERNS[field] for field,index in header.items()):
                header=None
            if header:
                flush()
                pending=None
                columns=header
                continue
            if columns is None:
                raise ValueError(f'{sheet_name}: recognizable Title, action and expected-result headers are required')
            def cell(field):
                index=columns.get(field)
                return cells[index] if index is not None and index<len(cells) else ''
            if cell('title'):
                flush()
                priority=cell('priority')
                pending={'title':cell('title'),'category':_category_from_tags(cell('tags')),
                         'priority':int(priority) if priority.isdigit() and len(priority)<8 else None,
                         'description':cell('description'),'steps':[]}
            if pending is None:
                raise ValueError(f'{sheet_name} row {number}: step has no test-case title')
            action,expected=cell('action'),cell('expected')
            if not action and not expected:
                continue  # ADO may put metadata on a separate first row.
            if not action or not expected:
                raise ValueError(f'{sheet_name} row {number}: both step action and expected result are required')
            pending['steps'].append({'action':action,'expected':expected})
        flush()
    return cases


def parse_test_cases(text):
    """Legacy text-table path. Ambiguous/truncated text is rejected, not guessed."""
    if '[...truncated' in text:
        return [],'This import was truncated. Import the original CSV/XLSX or split it into smaller complete files.'
    sheets=[]
    rows=[]
    width=None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith('--- Sheet:'):
            if rows:sheets.append(('Sheet',rows))
            rows=[];width=None
            continue
        cells=[cell.strip() for cell in line.split(' | ')]
        if _match_header(cells):
            width=len(cells)
        elif width and len(cells)>width:
            return [],'Ambiguous pipe-delimited row. Import the original CSV/XLSX to preserve cell contents.'
        rows.append(cells)
    if rows:sheets.append(('Table',rows))
    try:
        cases=parse_rows(sheets)
        return (cases,None) if cases else ([],"No usable test cases found; check the Title and step action/expected headers.")
    except ValueError as exc:
        return [],str(exc)


def parse_file(filename,data):
    from . import doc_extract
    extension=doc_extract._extension_of(filename)
    try:
        if extension in ('.csv','.xlsx'):
            cases=parse_rows(doc_extract.extract_table(filename,data))
            return (cases,None) if cases else ([], 'No usable test cases found.')
        return parse_test_cases(doc_extract.extract_text(filename,data))
    except (ValueError,doc_extract.UnsupportedDocumentError) as exc:
        return [],str(exc)
