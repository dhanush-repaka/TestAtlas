"""Exports a repo's generated test cases to files people can take elsewhere.

Three formats, all built from the stored cases (server/db.py's test_cases) plus the
plain-English module names, so a reader sees "Shopping Cart", not `components.cart`:

  csv   Azure DevOps Test Plans' import shape: one row per test case carrying step 1,
        then one row per further step -- import it via Test Plans > Import, or open it
        in Excel. UTF-8 with a BOM so Excel doesn't garble non-ASCII text.
  md    a readable document: one section per module, a steps table per case.
  json  everything, as stored, for scripts and other tools.

Older cases (from before functional generation) keep plain-string steps and one
overall expected result; they're exported too, with the expected result on the
final step so every format has the same shape.

Every case carries TWO independent traceability signals -- a case generated from
a user story may have no code module, and one generated from code may have no
user story behind it, so each is reported on its own rather than one hiding the
other's absence:
  code traceability    the module and real code names (functions/classes/methods)
                        this case cites -- or an explicit "not linked" line if it
                        cites none.
  user story traceability  the document(s) (or journey) this case is linked to --
                        directly, if it was generated from one, or via a shared
                        code citation with an already-judged Story Coverage
                        requirement (the same signal the Test Cases tab shows as
                        "related requirements") -- or an explicit "not linked"
                        line if neither applies. `documents` and `journeys` (id ->
                        name maps, from server/db.py's list_documents/list_journeys)
                        resolve these to readable names.
"""
from __future__ import annotations

import csv
import io
import json
import re

FORMATS = {
    "csv": ("text/csv; charset=utf-8", "csv"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "json": ("application/json", "json"),
}

CSV_COLUMNS = ["ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected",
               "Priority", "Description", "Tags", "Area Path", "State"]

_CATEGORY_LABELS = {"happy_path": "Happy path", "edge_case": "Edge case", "error_handling": "Error handling"}


def _steps(case: dict) -> list[dict]:
    """[{action, expected}] whichever shape the case was stored in."""
    out = []
    raw = case.get("steps") or []
    for i, s in enumerate(raw):
        if isinstance(s, dict):
            out.append({"action": str(s.get("action") or ""), "expected": str(s.get("expected") or "")})
        else:  # legacy: plain-text step, one overall expected result for the case
            last = i == len(raw) - 1
            out.append({"action": str(s), "expected": str(case.get("expected_result") or "") if last else ""})
    return out


def _module_name(case: dict, labels: dict) -> str:
    module = case.get("module") or ""
    return (labels.get(module) or {}).get("name") or module


def _safe_cell(value) -> str:
    """Spreadsheet formula injection: a cell starting with = + - @ is run as a formula by
    Excel/Sheets. Test text comes from a model that read arbitrary source code, so it's
    neutralised with a leading apostrophe (shown as plain text, not as part of the value)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _code_traceability(case: dict) -> str:
    """Which code this case exercises: the module's technical (dotted) name -- the display
    name in Tags is for people, this is what to search for in the repo -- and the real
    function/class/`Class.method` names the model cited. Explicit "not linked" line, never
    blank, so a reader can tell "no code citations" apart from "I forgot to check"."""
    covers = [c for c in (case.get("covers") or []) if c]
    module = case.get("module") or ""
    if not covers and not module:
        return "Code traceability: not linked to any code module"
    line = f"Code traceability: module {module}" if module else "Code traceability:"
    return f"{line}; covers {', '.join(covers)}" if covers else line


def _story_entries(case: dict, documents: dict, journeys: dict) -> list[dict]:
    """Every user story (or journey) this case is linked to: directly, if it was
    generated from one (source_doc_id/source_journey_id), plus any document whose
    requirement shares a code citation with this case (case["related_requirements"],
    the same heuristic server/llm_story_coverage.py's link_requirements_to_cases
    computes for the Test Cases tab) -- so a code-driven case still surfaces the
    user story its code citations happen to back, even with no direct link."""
    entries = []
    seen_doc_ids = set()
    if case.get("source_journey_id"):
        jid = case["source_journey_id"]
        entries.append({"kind": "journey", "id": jid, "name": journeys.get(jid) or "an unnamed journey"})
    if case.get("source_doc_id"):
        did = case["source_doc_id"]
        entries.append({"kind": "document", "id": did, "name": documents.get(did) or "an unknown document"})
        seen_doc_ids.add(did)
    for r in case.get("related_requirements") or []:
        did = r.get("doc_id")
        if did and did not in seen_doc_ids:
            seen_doc_ids.add(did)
            entries.append({"kind": "document", "id": did, "name": documents.get(did) or "an unknown document"})
    return entries


def _story_traceability(case: dict, documents: dict, journeys: dict) -> str:
    """Explicit "not linked" line, never blank, mirroring _code_traceability --
    a user-story-driven case with no code module and a code-driven case with no
    matching user story must each say so, not just omit the other side."""
    entries = _story_entries(case, documents, journeys)
    if not entries:
        return "User story traceability: not linked to any user story"
    names = ", ".join('"{}"'.format(e["name"]) for e in entries)
    return f"User story traceability: {names}"


def _description(case: dict, documents: dict, journeys: dict) -> str:
    """Description for the CSV. Traceability rides here rather than in a new column:
    Description is a field every ADO test case has, so the import can't reject it the way
    it may reject an unrecognised column."""
    parts = [str(case.get("description") or "").strip()]
    if (case.get("preconditions") or "").strip():
        parts.append(f"Preconditions: {case['preconditions'].strip()}")
    parts.append(_code_traceability(case))
    parts.append(_story_traceability(case, documents, journeys))
    return "\n".join(p for p in parts if p)


def to_csv(cases: list[dict], labels: dict, documents: dict | None = None, journeys: dict | None = None) -> str:
    documents = documents or {}
    journeys = journeys or {}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for c in cases:
        steps = _steps(c) or [{"action": "", "expected": ""}]
        tags = "; ".join(t for t in (_module_name(c, labels), _CATEGORY_LABELS.get(c.get("category"), c.get("category")), c.get("feature")) if t)
        w.writerow([
            "", "Test Case", _safe_cell(c.get("title")), 1, _safe_cell(steps[0]["action"]), _safe_cell(steps[0]["expected"]),
            c.get("priority") or "", _safe_cell(_description(c, documents, journeys)), _safe_cell(tags), "", "Design",
        ])
        for n, s in enumerate(steps[1:], 2):
            w.writerow(["", "", "", n, _safe_cell(s["action"]), _safe_cell(s["expected"]), "", "", "", "", ""])
    return "\ufeff" + buf.getvalue()


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def to_markdown(cases: list[dict], labels: dict, repo_name: str, documents: dict | None = None, journeys: dict | None = None) -> str:
    documents = documents or {}
    journeys = journeys or {}
    lines = [f"# Test cases: {repo_name}", "", f"{len(cases)} test case{'s' if len(cases) != 1 else ''}.", ""]
    by_module: dict[str, list[dict]] = {}
    for c in cases:
        by_module.setdefault(c.get("module") or "", []).append(c)
    for module, group in sorted(by_module.items(), key=lambda kv: _module_name(kv[1][0], labels).lower()):
        info = labels.get(module) or {}
        lines += [f"## {_module_name(group[0], labels)}", ""]
        if info.get("description"):
            lines += [f"_{info['description']}_", ""]
        for c in group:
            meta = " · ".join(x for x in (
                f"Priority {c['priority']}" if c.get("priority") else "", _CATEGORY_LABELS.get(c.get("category"), ""), c.get("feature") or "") if x)
            lines += [f"### {c.get('title')}", "", meta, ""] if meta else [f"### {c.get('title')}", ""]
            if (c.get("description") or "").strip():
                lines += [c["description"].strip(), ""]
            if (c.get("preconditions") or "").strip():
                lines += [f"**Preconditions:** {c['preconditions'].strip()}", ""]
            lines += [f"_{_code_traceability(c)}_", ""]
            lines += [f"_{_story_traceability(c, documents, journeys)}_", ""]
            lines += ["| # | Action | Expected result |", "|---|--------|-----------------|"]
            lines += [f"| {n} | {_md_cell(s['action'])} | {_md_cell(s['expected'])} |" for n, s in enumerate(_steps(c), 1)]
            lines.append("")
    return "\n".join(lines)


def to_json(cases: list[dict], labels: dict, documents: dict | None = None, journeys: dict | None = None) -> str:
    documents = documents or {}
    journeys = journeys or {}
    out = []
    for c in cases:
        covers = c.get("covers") or []
        module = c.get("module") or None
        story_entries = _story_entries(c, documents, journeys)
        out.append({
            "title": c.get("title"), "description": c.get("description"), "priority": c.get("priority"),
            "category": c.get("category"), "feature": c.get("feature"), "preconditions": c.get("preconditions"),
            "module": module, "module_name": _module_name(c, labels),
            "steps": _steps(c), "covers": covers,
            "code_traceability": {"linked": bool(module or covers), "module": module, "covers": covers},
            "user_story_traceability": {"linked": bool(story_entries), "stories": story_entries},
        })
    return json.dumps({"test_cases": out}, indent=2, ensure_ascii=False)


def filename(repo_name: str, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", repo_name).strip("-").lower() or "repo"
    return f"{slug}-test-cases.{ext}"
