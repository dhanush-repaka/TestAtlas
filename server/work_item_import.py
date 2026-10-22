"""Splits an ADO-style work-item export (already extracted to a flat,
pipe-delimited-row text by server/doc_extract.py -- the SAME shape whether
the upload was a real .csv or an .xlsx) into one TestAtlas document per row,
so Story Coverage's per-document decomposition sees one user story at a
time -- small and reliable, the same shape every demo document already
works with -- instead of being asked to correctly separate dozens of
unrelated stories out of one blob in a single call. See server/app.py's
/documents/import-work-items route.

Recognizes a header row by column NAME (case-insensitive substring match),
tolerant of whatever exact names a real export uses ("ID" vs "Work Item ID",
"Acceptance Criteria" vs the raw API's "Microsoft.VSTS.Common.AcceptanceCriteria").
If no recognizable header is found at all, there's nothing to safely split
on -- falls back to treating the whole upload as ONE document (today's
existing single-document behavior) rather than guessing wrong and silently
losing data.
"""
from __future__ import annotations

# field -> patterns matched as a case-insensitive SUBSTRING of a header cell,
# so both a friendly export name and a raw API field name resolve the same way.
_COLUMN_PATTERNS: dict[str, list[str]] = {
    "id": ["id"],
    "title": ["title"],
    "description": ["description"],
    "acceptance_criteria": ["acceptance criteria", "acceptancecriteria"],
}

# Need at least an identifying field plus one content field before trusting
# that a header row is actually a work-item export and not just prose that
# happens to contain the word "title".
_MIN_RECOGNIZED_COLUMNS = 2

_MAX_NAME_LENGTH = 200


def _match_header(header_cells: list[str]) -> dict[str, int] | None:
    """{field: column index} for whichever _COLUMN_PATTERNS fields this
    header recognizes, or None if too few match to be confident."""
    found: dict[str, int] = {}
    for i, cell in enumerate(header_cells):
        low = cell.lower().strip()
        for field, patterns in _COLUMN_PATTERNS.items():
            if field in found:
                continue
            if any(p in low for p in patterns):
                found[field] = i
                break
    return found if len(found) >= _MIN_RECOGNIZED_COLUMNS else None


def split_work_items(text: str) -> list[dict]:
    """Returns [{"name": ..., "content": ...}, ...], one per recognized row.
    Returns [{"name": None, "content": text}] (the caller falls back to
    "use the uploaded filename", same as today's single-document upload)
    when no recognizable header exists -- never raises, never drops the
    upload's content on an unrecognized shape."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    header_cells = [c.strip() for c in lines[0].split(" | ")]
    columns = _match_header(header_cells)
    if not columns:
        return [{"name": None, "content": text}]

    def cell(cells: list[str], field: str) -> str:
        idx = columns.get(field)
        return cells[idx] if idx is not None and idx < len(cells) else ""

    items: list[dict] = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(" | ")]
        if not any(cells):
            continue

        item_id = cell(cells, "id")
        title = cell(cells, "title")
        description = cell(cells, "description")
        acceptance_criteria = cell(cells, "acceptance_criteria")

        if not title and not description and not acceptance_criteria:
            continue  # a blank or malformed row -- nothing to build a document from

        name = f"{item_id}: {title}" if item_id and title else (title or item_id or "Untitled work item")
        content_parts = [p for p in (
            title,
            description,
            f"Acceptance Criteria:\n{acceptance_criteria}" if acceptance_criteria else "",
        ) if p]
        items.append({"name": name[:_MAX_NAME_LENGTH], "content": "\n\n".join(content_parts)})
    return items
