"""Rolls up a repo's already-computed Story Coverage and test-case data into one
project-status summary a PRODUCT OWNER can actually use: how much of the scope is
built, how much of what's built is actually correct, and what's broken -- not an
implementation-detail readout. See server/app.py's GET /repos/{repo_id}/dashboard.

Pure computation, no LLM calls, nothing stored beyond the daily trend snapshot
(server/db.py's dashboard_snapshots) -- everything here is derived fresh, at read
time, from data other features already produced:
  - server/llm_story_coverage.py's compute_tested_status (story -> test direction)
    and link_requirements_to_cases (test -> story direction, including its
    worst-of-matches derived status for a pure code-driven case) are REUSED, not
    reimplemented -- the exact same linking logic server/test_export.py's
    traceability already relies on.
  - module coverage (code -> test direction) is new here: for every module the
    latest successful run's own module_scores knows about, does at least one
    stored test case cite it.

Deliberately does NOT surface test-case origin (code-driven vs story-driven vs
journey-driven) anywhere in this summary -- that's how a case was generated, an
implementation detail of this tool, not a signal about the PROJECT'S status. A
product owner doesn't care how a test was written, only whether the story behind
it is actually built and actually correct.
"""
from __future__ import annotations

from server.llm_story_coverage import compute_tested_status, link_requirements_to_cases

_REQUIREMENT_STATUSES = ("supported", "contradicted", "unknown", "needs_human_input")

# A user story's real-world status, in priority order (most severe first) -- every
# story lands in exactly ONE bucket, so the buckets sum to the total story count.
# This is the dashboard's headline view: a product owner thinks in stories, not
# acceptance criteria, and wants "where do we actually stand", not a raw count.
_STORY_HEALTH_LABELS = {
    "not_analyzed": "Not analyzed yet",
    "broken": "Built but broken",
    "needs_clarification": "Needs clarification",
    "not_started": "Not started",
    "partially_built": "Partially built",
    "verified": "Built and verified correct",
}


def _status_counts(requirements: list[dict]) -> dict[str, int]:
    counts = {s: 0 for s in _REQUIREMENT_STATUSES}
    for r in requirements:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    return counts


def _pct(n: int, d: int) -> float:
    return round((n / d) * 100, 1) if d else 0.0


def _count_by(items: list[dict], field: str) -> dict:
    """Ordered {value: count} for a field, skipping items where it's unset --
    used for the test-case category/priority breakdown, a real quality signal
    (is testing well-rounded or just covering the happy path) distinct from
    the already-rejected "origin" stat (how a case was generated, not what
    kind of case it is)."""
    counts: dict = {}
    for it in items:
        v = it.get(field)
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    return counts


# How many of a repo's modules count as "critical" for the dashboard's risk
# framing -- matches the existing "most critical module" concept this app
# already shows on each repo's tile (static/app.js's repoTileHtml, keyed off
# module_scores[0], the single highest-PageRank module after sorting): a
# small, fixed number of structurally central modules, not a percentage that
# would scale strangely on a tiny repo.
_CRITICAL_MODULE_COUNT = 3


def _critical_modules(module_scores: list[dict]) -> set[str]:
    """The repo's most structurally central modules by PageRank -- "central"
    here means most of the codebase imports from it, not "important to the
    business" (this tool has no way to know that), but it's the same honest
    proxy the rest of this app already uses for "critical"."""
    ranked = sorted((m for m in module_scores if m.get("module")), key=lambda m: m.get("pagerank", 0), reverse=True)
    return {m["module"] for m in ranked[:_CRITICAL_MODULE_COUNT]}


def _overall_status(health_counts: dict[str, int], total_docs: int, verified_pct: float, critical_broken: int) -> dict:
    """The dashboard's single headline verdict -- a product owner scanning for
    five seconds should get "are we okay or not" from this alone, with the one
    sentence underneath explaining why. `red` if ANYTHING is confirmed broken
    (a single broken story is worth surfacing loudly, regardless of how many
    others are fine); `green` only once every story is actually verified
    correct, not just coded; everything else is `amber` -- genuinely in
    progress, nothing on fire yet."""
    broken = health_counts["broken"]
    if total_docs == 0:
        return {"status": "amber", "headline": "No user stories yet -- add one in the Docs tab."}
    if broken > 0:
        critical_note = ""
        if critical_broken > 0:
            critical_note = f" {critical_broken} {'touches' if critical_broken == 1 else 'touch'} your most critical code."
        return {"status": "red", "headline": (
            f"{health_counts['verified']} of {total_docs} stories are fully delivered and verified. "
            f"{broken} {'is' if broken == 1 else 'are'} confirmed broken.{critical_note}"
        )}
    if verified_pct >= 100:
        return {"status": "green", "headline": f"All {total_docs} user stories are fully delivered and verified correct."}
    return {"status": "amber", "headline": (
        f"{health_counts['verified']} of {total_docs} stories are fully delivered and verified. "
        f"The rest are still in progress."
    )}


def _story_health(by_status: dict[str, int], requirement_count: int) -> str:
    """One bucket per story, most-severe-first: a story with even one contradicted
    criterion is "broken" regardless of how many others are fine -- a product
    owner needs to see the worst thing true about a story, not an average that
    hides it. "not_started" means every criterion came back with literally no
    code found (see has_code's own docstring: that's what "unknown" always
    means) -- distinct from "partially_built", where SOME criteria have real
    code behind them and others don't yet."""
    if requirement_count == 0:
        return "not_analyzed"
    if by_status["contradicted"] > 0:
        return "broken"
    if by_status["needs_human_input"] > 0:
        return "needs_clarification"
    if by_status["unknown"] == requirement_count:
        return "not_started"
    if by_status["unknown"] > 0:
        return "partially_built"
    return "verified"


def compute_summary(
    documents: list[dict],
    requirements: list[dict],
    test_cases: list[dict],
    module_scores: list[dict],
    module_labels: dict[str, dict],
) -> dict:
    """`documents`/`requirements`/`test_cases` -- server/db.py's list_documents/
    list_story_requirements/list_test_cases, unfiltered (every document in the
    repo). `module_scores` -- the latest successful run's own
    stats["module_scores"] (db.get_latest_successful_run already parses this),
    or [] if there's no successful run yet -- code coverage is one section of
    the dashboard, not a precondition for the story/test sections. `module_labels`
    -- db.get_module_label_details, for display names in the gap list."""
    tested_requirements = compute_tested_status(requirements, test_cases)
    tested_by_id = {r["id"]: r for r in tested_requirements}
    linked_cases = link_requirements_to_cases(test_cases, requirements)
    critical_modules = _critical_modules(module_scores)

    # A document "has test coverage" if either direction confirms it: one of its
    # own requirements shows tested=True (citation overlap), OR a case is
    # DIRECTLY sourced from it (link_requirements_to_cases resolves source_doc_id
    # even for an older requirement-sourced case that predates that column) --
    # the second case matters because a whole-document-generated case can exist
    # with covers=[] (genuinely unverified) and so would never show up via the
    # first path alone.
    direct_case_docs: set[str] = {c["source_doc_id"] for c in linked_cases if c.get("source_doc_id")}
    cases_by_doc: dict[str, list[dict]] = {}
    for c in linked_cases:
        if c.get("source_doc_id"):
            cases_by_doc.setdefault(c["source_doc_id"], []).append(c)
    reqs_by_doc: dict[str, list[dict]] = {}
    for r in requirements:
        if r.get("doc_id"):
            reqs_by_doc.setdefault(r["doc_id"], []).append(r)

    document_rollup = []
    for d in documents:
        doc_reqs = reqs_by_doc.get(d["id"], [])
        tested_reqs = [tested_by_id[r["id"]] for r in doc_reqs if r["id"] in tested_by_id]
        tested_count = sum(1 for r in tested_reqs if r["tested"])
        doc_cases = cases_by_doc.get(d["id"], [])
        # Modules this story touches: its own directly-sourced cases, plus any
        # case whose heuristic related_requirements include one of this doc's
        # own requirements -- a code-driven case can legitimately back a story
        # it was never generated from.
        modules: set[str] = {c["module"] for c in doc_cases if c.get("module")}
        for c in linked_cases:
            if c.get("module") and any(
                m.get("doc_id") == d["id"] for m in (c.get("related_requirements") or [])
            ):
                modules.add(c["module"])
        has_test_coverage = tested_count > 0 or d["id"] in direct_case_docs
        # "Coded": at least one of this story's own criteria was actually matched
        # against real code (covers non-empty -- the judge only reaches a verdict
        # of supported/contradicted once it has something to cite; "unknown"
        # always means no matching code was found at all, never a weak match).
        has_code = any(r.get("covers") for r in doc_reqs)
        by_status = _status_counts(doc_reqs)
        health = _story_health(by_status, len(doc_reqs))
        touches_critical_code = bool(modules & critical_modules)
        document_rollup.append({
            "id": d["id"], "name": d["name"],
            "requirement_count": len(doc_reqs),
            "by_status": by_status,
            "health": health,
            "tested_count": tested_count,
            "test_case_count": len(doc_cases),
            "has_test_coverage": has_test_coverage,
            "has_code": has_code,
            "modules": sorted(modules),
            "touches_critical_code": touches_critical_code,
        })

    total_docs = len(documents)
    with_reqs = sum(1 for d in document_rollup if d["requirement_count"] > 0)
    with_tests = sum(1 for d in document_rollup if d["has_test_coverage"])
    with_code = sum(1 for d in document_rollup if d["has_code"])
    health_counts = {k: 0 for k in _STORY_HEALTH_LABELS}
    for d in document_rollup:
        health_counts[d["health"]] += 1
    critical_broken = sum(1 for d in document_rollup if d["health"] == "broken" and d["touches_critical_code"])
    critical_at_risk = sum(1 for d in document_rollup if d["health"] != "verified" and d["touches_critical_code"])

    module_names = [m["module"] for m in module_scores if m.get("module")]
    tested_modules = {c["module"] for c in test_cases if c.get("module")}
    modules_without = [
        {"module": m, "display_name": (module_labels.get(m) or {}).get("name") or m}
        for m in module_names if m not in tested_modules
    ]

    doc_by_id = {d["id"]: d for d in documents}
    rollup_by_doc_id = {d["id"]: d for d in document_rollup}

    def _req_entry(r: dict) -> dict:
        doc = doc_by_id.get(r.get("doc_id"))
        rollup = rollup_by_doc_id.get(r.get("doc_id"))
        return {
            "id": r["id"], "doc_id": r.get("doc_id"), "doc_name": doc["name"] if doc else None,
            "text": r["text"], "rationale": r.get("rationale"), "question": r.get("question"),
            "touches_critical_code": bool(rollup and rollup["touches_critical_code"]),
        }

    req_status = _status_counts(requirements)
    total_reqs = len(requirements)
    # "Correctness rate": of criteria that were actually CODED (judged against
    # real code -- supported or contradicted, never unknown, see has_code's own
    # note), what fraction came back right. This is the number that answers "of
    # what we've built, how much of it actually works as specified" -- contradiction
    # rate alone (contradicted / ALL criteria) conflates "wrong" with "not built
    # yet", which understates how bad the coded portion actually is on an
    # early-stage project, and overstates it on a mature one.
    coded_reqs = req_status["supported"] + req_status["contradicted"]
    verified_pct = _pct(health_counts["verified"], total_docs)
    return {
        "overall": _overall_status(health_counts, total_docs, verified_pct, critical_broken),
        "documents": {
            "total": total_docs,
            "with_requirements": with_reqs,
            "without_requirements": total_docs - with_reqs,
            "with_test_coverage": with_tests,
            "without_test_coverage": total_docs - with_tests,
            "with_code": with_code,
            "without_code": total_docs - with_code,
            "coded_pct": _pct(with_code, total_docs),
            "tested_pct": _pct(with_tests, total_docs),
            "by_health": health_counts,
            "verified_pct": verified_pct,
            "critical_at_risk": critical_at_risk,
        },
        "requirements": {
            "total": total_reqs,
            "by_status": req_status,
            "contradiction_rate": _pct(req_status["contradicted"], total_reqs),
            "supported_rate": _pct(req_status["supported"], total_reqs),
            "correctness_rate": _pct(req_status["supported"], coded_reqs),
        },
        "test_cases": {
            "total": len(test_cases),
            "by_category": _count_by(test_cases, "category"),
            "by_priority": _count_by(test_cases, "priority"),
        },
        "modules": {
            "total": len(module_names),
            "with_coverage": len(module_names) - len(modules_without),
            "without_coverage": len(modules_without),
            "coverage_pct": _pct(len(module_names) - len(modules_without), len(module_names)),
            "critical": [
                {"module": m, "display_name": (module_labels.get(m) or {}).get("name") or m}
                for m in module_names if m in critical_modules
            ],
        },
        "gaps": {
            "contradicted_requirements": [_req_entry(r) for r in requirements if r.get("status") == "contradicted"],
            "needs_human_input_requirements": [_req_entry(r) for r in requirements if r.get("status") == "needs_human_input"],
            "documents_not_analyzed": [{"id": d["id"], "name": d["name"]} for d in document_rollup if d["requirement_count"] == 0],
            "documents_without_tests": [{"id": d["id"], "name": d["name"]} for d in document_rollup if not d["has_test_coverage"]],
            "documents_without_code": [{"id": d["id"], "name": d["name"]} for d in document_rollup if d["requirement_count"] > 0 and not d["has_code"]],
            "modules_without_tests": modules_without,
        },
        "document_rollup": document_rollup,
    }
