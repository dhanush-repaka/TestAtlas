"""Extracts plain text from uploaded documents in whatever format a real
project's documentation actually happens to be in -- not just plain
text/Markdown. All extraction is local, pure-Python, and free (no API calls),
consistent with the rest of this project's cost constraints.
"""
from __future__ import annotations

import io

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".docx", ".pptx", ".xlsx", ".pdf"}

_MAX_CHARS = 300_000  # generous, but keeps a pathological huge file from blowing up
                       # the DB row / gap-analysis context size


class UnsupportedDocumentError(ValueError):
    pass


def _extension_of(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def extract_text(filename: str, data: bytes) -> str:
    """Returns the document's text content. Raises UnsupportedDocumentError
    (safe to show directly to the user) for an unrecognized extension, or if
    extraction produced nothing usable."""
    ext = _extension_of(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocumentError(
            f"Unsupported file type '{ext or filename}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if ext in (".txt", ".md", ".markdown"):
        text = data.decode("utf-8", errors="replace")
    elif ext == ".docx":
        text = _extract_docx(data)
    elif ext == ".pptx":
        text = _extract_pptx(data)
    elif ext == ".xlsx":
        text = _extract_xlsx(data)
    else:  # .pdf
        text = _extract_pdf(data)

    text = text.strip()
    if not text:
        raise UnsupportedDocumentError("Couldn't find any text in that file -- it may be empty, image-only, or corrupted.")
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n\n[...truncated -- document exceeded the size this feature keeps in one document...]"
    return text


def _extract_docx(data: bytes) -> str:
    import docx  # lazy import: only needed when a .docx is actually uploaded

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation  # lazy import

    presentation = Presentation(io.BytesIO(data))
    slides_text = []
    for i, slide in enumerate(presentation.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
        if parts:
            slides_text.append(f"--- Slide {i} ---\n" + "\n".join(parts))
    return "\n\n".join(slides_text)


def _extract_xlsx(data: bytes) -> str:
    import openpyxl  # lazy import

    workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheets_text = []
    for ws in workbook.worksheets:
        rows_text = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                rows_text.append(" | ".join(cells))
        if rows_text:
            sheets_text.append(f"--- Sheet: {ws.title} ---\n" + "\n".join(rows_text))
    return "\n\n".join(sheets_text)


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader  # lazy import

    reader = PdfReader(io.BytesIO(data))
    pages_text = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(t for t in pages_text if t)
