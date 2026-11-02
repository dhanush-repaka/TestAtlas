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

Deliberately does NOT surface GENERATION-MECHANISM origin (code-driven vs
story-driven vs journey-driven) anywhere in this summary -- that's how a
generated case was produced, an implementation detail of this tool, not a
signal about the PROJECT'S status. A product owner doesn't care how a test
was written, only whether the story behind it is actually built and
actually correct.

IMPORTED-vs-generated (test_cases.by_origin / documents.by_origin,
server/test_case_import.py and server/work_item_import.py's created_via
column) is a DIFFERENT, deliberately-surfaced signal: once a real suite can
be imported, "how much of this is inherited/manual vs. AI-assisted" is a
genuine stakeholder question, not an implementation detail -- distinct from
the still-hidden generation-mechanism distinction above. The two coverage
splits (test_cases.story_link_pct/code_coverage_pct) are ALSO broken out
per-origin (story_link_by_origin/code_coverage_by_origin), since a blended
percentage can hide a legacy imported suite's real coverage dragging down
(or propping up) freshly generated cases' own number.

This also deliberately keeps the dashboard useful for a repo with zero user
stories tracked in TestAtlas but many test cases (a legacy codebase that
only ever gets its existing suite imported, never any stories entered) --
see _overall_status's own docstring for how the headline adapts to that case.
"""
from __future__ import annotations

from server import evidence_rules
from server.llm_story_coverage import compute_tested_status, link_requirements_to_cases

_REQUIREMENT_STATUSES = ("supported", "contradicted", "unknown", "needs_human_input")

# A user story's real-world status, in priority order (most severe first) -- every
# story lands in exactly ONE bucket, so the buckets sum to the total story count.
# This is the dashboard's headline view: a product owner thinks in stories, not
# acceptance criteria, and wants "where do we actually stand", not a raw count.
_STORY_HEALTH_LABELS = {
    "not_analyzed": "Not analyzed yet",
    "broken": "Potential implementation conflict",
    "needs_clarification": "Needs clarification",
    "not_started": "No implementation evidence found",
    # NOT "Partially built" -- some criteria are unknown (no evidence found),
    # which is not the same claim as "not yet built" (server/evidence_rules.py's
    # own evidence_level makes the same distinction: insufficient_evidence is
    # never treated as "incomplete", just "nothing to judge from").
    "partially_built": "Partially supported",
    # A story whose criteria are ALL supported, but at least one of those
    # supported judgments is stale (its run_id predates the repo's latest
    # analysis run) -- NEVER counted as "verified", since trusting an old
    # judgment without saying so is exactly the false-confidence risk this
    # whole label set exists to avoid. See _story_health's has_stale param.
    "needs_reassessment": "Needs reassessment (stale)",
    # Only possible from _ai_status_counts' AI-only view, never the live one --
    # every one of this story's requirements predates ai_status/ai_covers, so
    # there's no frozen AI judgment left to classify it by at all.
    "ai_assessment_unavailable": "Original AI assessment unavailable",
    "verified": "Implementation supported by AI review",
}


def _status_counts(requirements: list[dict]) -> dict[str, int]:
    counts = {s: 0 for s in _REQUIREMENT_STATUSES}
    for r in requirements:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    return counts


def _ai_status_counts(requirements: list[dict]) -> tuple[dict[str, int], int]:
    """Like _status_counts, but keyed on evidence_rules.effective_ai_status --
    the AI's own FROZEN judgment -- instead of the live, human-mutable
    `status` a confirmation overwrites. A requirement with no AI snapshot at
    all (a legacy row predating ai_status) counts toward neither "unknown"
    nor any other named bucket -- "we don't know what the AI said" is a
    different fact from "the AI looked and found nothing" and must never be
    silently folded into either; it's returned separately as the second
    element, read by _story_health as `unavailable_count`."""
    counts = {s: 0 for s in _REQUIREMENT_STATUSES}
    unavailable = 0
    for r in requirements:
        status = evidence_rules.effective_ai_status(r)
        if status is None:
            unavailable += 1
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts, unavailable


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


def _is_story_linked(case: dict) -> bool:
    """A case counts as story-linked if it has a real generation-time source
    (source_doc_id/source_requirement_id/source_journey_id) OR
    link_requirements_to_cases' own citation-overlap heuristic already
    attached a related requirement -- the same two-direction definition
    document_rollup's has_test_coverage already uses, just checked from the
    test-case side instead of the story side. Expects a case from
    linked_cases (not raw test_cases), which carries the resolved
    source_doc_id and related_requirements."""
    return bool(
        case.get("source_doc_id") or case.get("source_requirement_id") or case.get("source_journey_id")
        or case.get("related_requirements")
    )


def _evidence_summary(
    items: list[dict], latest_run_id: str | None, reviews_by_target: dict[str, dict], use_ai_snapshot: bool = False,
) -> dict:
    """Stage 4's replacement for a single blended coverage percentage: an
    explicit count per server/evidence_rules.py evidence level, plus how many
    are stale (judged against an analysis run that's no longer the latest)
    and how many have a recorded human review -- shown alongside the AI
    level, never collapsed into it (see evidence_rules's own docstring).

    `use_ai_snapshot` -- pass True for story_requirements (never for test
    cases, whose status is never overwritten by human review in the first
    place): reads evidence_rules.ai_evidence_level (the FROZEN ai_status/
    ai_covers) instead of evidence_level (the live, human-mutable fields),
    so "ai_supported"/"ai_conflict" here can never be moved by a confirm.
    Adds two extra keys only in that mode: `human_confirmed` (source ==
    'human_confirmed' -- the CURRENT, possibly-different answer, tracked
    separately rather than silently blended into the ai_* counts) and
    `ai_assessment_unavailable` (a legacy row with no frozen snapshot at
    all, excluded from the ai_* counts rather than guessed at)."""
    counts = {level: 0 for level in evidence_rules.EVIDENCE_LEVELS}
    stale = 0
    human_reviewed = 0
    human_confirmed = 0
    ai_unavailable = 0
    for item in items:
        level = evidence_rules.ai_evidence_level(item) if use_ai_snapshot else evidence_rules.evidence_level(item)
        if level == evidence_rules.AI_ASSESSMENT_UNAVAILABLE:
            ai_unavailable += 1
        else:
            counts[level] += 1
        if evidence_rules.is_stale(item, latest_run_id):
            stale += 1
        if item.get("id") in reviews_by_target:
            human_reviewed += 1
        if use_ai_snapshot and item.get("source") == "human_confirmed":
            human_confirmed += 1
    out = {**counts, "stale": stale, "human_reviewed": human_reviewed}
    if use_ai_snapshot:
        out["human_confirmed"] = human_confirmed
        out["ai_assessment_unavailable"] = ai_unavailable
    return out


def _linkage_breakdown(items: list[dict], is_linked, latest_run_id: str | None) -> dict:
    """Internally consistent fresh/stale counts for ONE linkage check (story
    link or code coverage), so a reader never has to reconcile a numerator
    computed one way against a denominator computed another:
      fresh_linked + fresh_unlinked == fresh
      fresh + stale == total
    `pct` is None (never 0) when there are no fresh items to judge -- "no
    current assessments" is a different fact from "0% linked", and
    collapsing the two would be exactly the false-confidence risk this
    whole plan exists to avoid. A stale item NEVER appears in fresh_linked
    or fresh_unlinked -- it's simply not current evidence either way."""
    total = len(items)
    stale = sum(1 for it in items if evidence_rules.is_stale(it, latest_run_id))
    fresh = total - stale
    fresh_linked = sum(1 for it in items if not evidence_rules.is_stale(it, latest_run_id) and is_linked(it))
    fresh_unlinked = fresh - fresh_linked
    pct = round((fresh_linked / fresh) * 100, 1) if fresh else None
    return {"total": total, "fresh": fresh, "stale": stale, "fresh_linked": fresh_linked,
            "fresh_unlinked": fresh_unlinked, "pct": pct}


def _linkage_breakdown_by_origin(items: list[dict], is_linked, latest_run_id: str | None) -> dict:
    """Same _linkage_breakdown shape, one per created_via bucket -- so the
    dashboard can show an imported legacy suite's real linkage separately
    from freshly generated cases, rather than one blended number that could
    hide one dragging the other down."""
    by_origin: dict[str, list[dict]] = {}
    for it in items:
        by_origin.setdefault(it.get("created_via") or "generated", []).append(it)
    return {origin: _linkage_breakdown(bucket, is_linked, latest_run_id) for origin, bucket in by_origin.items()}


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


def _overall_status(
    health_counts: dict[str, int], total_docs: int, verified_pct: float, critical_broken: int,
    total_cases: int = 0, code_coverage_pct: float = 0.0, total_stale: int = 0, human_confirmed_verified: int = 0,
) -> dict:
    """The dashboard's single headline verdict -- a product owner scanning for
    five seconds should get "are we okay or not" from this alone, with the one
    sentence underneath explaining why. `red` if ANYTHING is confirmed broken
    (a single broken story is worth surfacing loudly, regardless of how many
    others are fine); `green` only once every story is actually verified
    correct, not just coded; everything else is `amber` -- genuinely in
    progress, nothing on fire yet.

    `total_cases`/`code_coverage_pct` only matter for the zero-user-story
    case: a legacy repo can have hundreds of test cases (imported, or
    code-driven generated) with no user stories ever entered into TestAtlas
    at all -- "add one in the Docs tab" is useless advice there, and the rest
    of the dashboard would look empty even though there's real signal in the
    test-case-level numbers.

    `total_stale` (server/evidence_rules.py's is_stale, summed across test
    cases and requirements) never changes red/amber/green -- a stale
    assessment isn't itself bad news, it's a "this needs a fresh look"
    signal -- but it's named in the headline whenever nonzero rather than
    silently folded into either a good or bad count.

    `health_counts["verified"]` (and `verified_pct`) are the CURRENT state --
    AI review plus any human confirmation, combined, since a human-confirmed
    fix really is a fix and this headline answers "are we okay right now".
    That's a DIFFERENT number from documents.ai_verified_pct (strictly the
    AI's own frozen judgment, immune to confirmation) -- `human_confirmed_verified`
    (how many of the "verified" count got there via at least one human
    confirmation) is named explicitly here so the headline never implies
    "AI review" said something a human actually decided."""
    stale_note = f" {total_stale} assessment{'s' if total_stale != 1 else ''} {'are' if total_stale != 1 else 'is'} stale and need reassessment." if total_stale else ""
    confirmed_note = (
        f" {human_confirmed_verified} {'includes' if human_confirmed_verified == 1 else 'include'} at least one human-confirmed criterion."
        if human_confirmed_verified else ""
    )
    broken = health_counts["broken"]
    if total_docs == 0:
        if total_cases:
            return {"status": "amber", "headline": (
                f"No user stories tracked yet. {total_cases} test case{'s' if total_cases != 1 else ''} on file, "
                f"{code_coverage_pct}% linked to real code.{stale_note}"
            )}
        return {"status": "amber", "headline": f"No user stories yet -- add one in the Docs tab.{stale_note}"}
    if broken > 0:
        critical_note = ""
        if critical_broken > 0:
            critical_note = f" {critical_broken} {'touches' if critical_broken == 1 else 'touch'} your most structurally central code."
        return {"status": "red", "headline": (
            f"{health_counts['verified']} of {total_docs} stories have implementation supported by AI review or human confirmation. "
            f"{broken} {'is' if broken == 1 else 'are'} a potential implementation conflict.{critical_note}{stale_note}{confirmed_note}"
        )}
    if verified_pct >= 100:
        return {"status": "green", "headline": f"All {total_docs} user stories have implementation supported by AI review or human confirmation.{stale_note}{confirmed_note}"}
    return {"status": "amber", "headline": (
        f"{health_counts['verified']} of {total_docs} stories have implementation supported by AI review or human confirmation. "
        f"The rest are still in progress.{stale_note}{confirmed_note}"
    )}


def _story_health(
    by_status: dict[str, int], requirement_count: int, has_stale: bool = False, unavailable_count: int = 0,
) -> str:
    """One bucket per story, most-severe-first: a story with even one contradicted
    criterion is "broken" regardless of how many others are fine -- a product
    owner needs to see the worst thing true about a story, not an average that
    hides it. "not_started" means every criterion came back with literally no
    code found (see has_code's own docstring: that's what "unknown" always
    means) -- distinct from "partially_built", where SOME criteria have real
    code behind them and others don't yet.

    `has_stale` -- true when ANY of this story's requirements is stale
    (server/evidence_rules.py's is_stale against the repo's latest run) --
    demotes what would otherwise be "verified" to "needs_reassessment"
    instead: a story whose only supported judgments are stale must never
    read as done. Doesn't change "broken"/"needs_clarification"/etc., since
    those already read as NOT-done; the false-confidence risk is specifically
    a stale judgment silently counting as current good news.

    `unavailable_count` -- only nonzero when called with _ai_status_counts'
    AI-only view (never the live one): how many of this story's requirements
    have no frozen AI snapshot at all. `by_status`/`requirement_count` in
    that case only ever reflect the ASSESSED subset -- `unavailable_count`
    makes up the difference, and a story with nothing assessed at all (every
    requirement unavailable) gets its own bucket rather than being silently
    read as "not_started" (which would claim the AI looked and found
    nothing, when really there's no record of the AI looking at all)."""
    if requirement_count == 0:
        return "not_analyzed"
    assessed_count = requirement_count - unavailable_count
    if unavailable_count and assessed_count == 0:
        return "ai_assessment_unavailable"
    if by_status["contradicted"] > 0:
        return "broken"
    if by_status["needs_human_input"] > 0:
        return "needs_clarification"
    if by_status["unknown"] == assessed_count:
        return "not_started"
    if by_status["unknown"] > 0:
        return "partially_built"
    return "needs_reassessment" if has_stale else "verified"


def compute_summary(
    documents: list[dict],
    requirements: list[dict],
    test_cases: list[dict],
    module_scores: list[dict],
    module_labels: dict[str, dict],
    latest_run_id: str | None = None,
    human_reviews: list[dict] | None = None,
) -> dict:
    """`documents`/`requirements`/`test_cases` -- server/db.py's list_documents/
    list_story_requirements/list_test_cases, unfiltered (every document in the
    repo). `module_scores` -- the latest successful run's own
    stats["module_scores"] (db.get_latest_successful_run already parses this),
    or [] if there's no successful run yet -- code coverage is one section of
    the dashboard, not a precondition for the story/test sections. `module_labels`
    -- db.get_module_label_details, for display names in the gap list.
    `latest_run_id` (db.get_latest_successful_run's own id) and `human_reviews`
    (db.list_human_reviews) feed server/evidence_rules.py's staleness and
    human-review signals -- both optional/None-safe, since a repo with no
    successful run yet or no reviews recorded is a normal, common state."""
    # A document edit invalidates a three-way assessment even without a new code run.
    from .test_case_view import enrich_cases
    test_cases = enrich_cases(test_cases, latest_run_id, documents)
    reviews_by_target = evidence_rules.latest_review_by_target(human_reviews or [])
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
        has_stale = any(evidence_rules.is_stale(r, latest_run_id) for r in doc_reqs)
        health = _story_health(by_status, len(doc_reqs), has_stale)
        # Strictly-AI view of the SAME document -- never moves from a human
        # confirmation, only from a fresh AI judgment (server/app.py's
        # api_confirm_story_requirement overwrites status/covers but never
        # ai_status/ai_covers). Drives documents.ai_verified_pct, the thing
        # actually labeled "AI-supported" on the dashboard -- `health`/
        # `verified_pct` above stay CURRENT-state (AI + any human
        # confirmation combined), which is what the overall red/amber/green
        # status and gap lists use, since a human-confirmed fix really is a
        # fix and should stop showing as broken there.
        ai_by_status, ai_unavailable = _ai_status_counts(doc_reqs)
        ai_health = _story_health(ai_by_status, len(doc_reqs), has_stale, ai_unavailable)
        has_human_confirmed = any(r.get("source") == "human_confirmed" for r in doc_reqs)
        touches_critical_code = bool(modules & critical_modules)
        document_rollup.append({
            "id": d["id"], "name": d["name"],
            "requirement_count": len(doc_reqs),
            "by_status": by_status,
            "health": health,
            "ai_health": ai_health,
            "ai_unavailable_count": ai_unavailable,
            "has_human_confirmed": has_human_confirmed,
            "tested_count": tested_count,
            "test_case_count": len(doc_cases),
            "has_test_coverage": has_test_coverage,
            "has_code": has_code,
            "has_stale": has_stale,
            "modules": sorted(modules),
            "touches_critical_code": touches_critical_code,
        })

    total_docs = len(documents)
    with_reqs = sum(1 for d in document_rollup if d["requirement_count"] > 0)
    with_tests = sum(1 for d in document_rollup if d["has_test_coverage"])
    with_code = sum(1 for d in document_rollup if d["has_code"])
    health_counts = {k: 0 for k in _STORY_HEALTH_LABELS}
    ai_health_counts = {k: 0 for k in _STORY_HEALTH_LABELS}
    for d in document_rollup:
        health_counts[d["health"]] += 1
        ai_health_counts[d["ai_health"]] += 1
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

    # Every percentage below is computed over FRESH (non-stale) items only --
    # a stale "supported" requirement must not keep propping up a correctness
    # number any more than it props up a story's health (see has_stale above).
    # `by_status`/`total` stay full, unfiltered counts (an honest "how many
    # criteria exist" fact, not a rate) -- evidence.stale already reports how
    # many of them were excluded, so the two together still reconcile.
    #
    # contradiction_rate/supported_rate/correctness_rate are explicitly labeled
    # "AI-supported" on the dashboard (static/app.js's stat cards), so -- like
    # documents.ai_verified_pct below -- they're computed from the AI's own
    # FROZEN judgment (_ai_status_counts), fresh items only, never the live
    # status a human confirmation can overwrite. `ai_unavailable_reqs` (legacy
    # rows with no AI snapshot) are excluded from both the numerator and
    # denominator, same reasoning as review-accuracy's excluded_no_ai_judgment.
    fresh_requirements = [r for r in requirements if not evidence_rules.is_stale(r, latest_run_id)]
    req_status, ai_unavailable_reqs = _ai_status_counts(fresh_requirements)
    total_reqs = len(requirements)
    fresh_assessed_reqs = len(fresh_requirements) - ai_unavailable_reqs
    # "Correctness rate": of criteria that were actually CODED (judged against
    # real code -- supported or contradicted, never unknown, see has_code's own
    # note), what fraction came back right. This is the number that answers "of
    # what we've built, how much of it actually works as specified" -- contradiction
    # rate alone (contradicted / ALL criteria) conflates "wrong" with "not built
    # yet", which understates how bad the coded portion actually is on an
    # early-stage project, and overstates it on a mature one.
    coded_reqs = req_status["supported"] + req_status["contradicted"]
    verified_pct = _pct(health_counts["verified"], total_docs)
    # The thing literally labeled "AI-supported" -- strictly the AI's own
    # frozen judgment, see document_rollup's ai_health comment above.
    ai_verified_pct = _pct(ai_health_counts["verified"], total_docs)

    total_cases = len(test_cases)  # the raw count -- also feeds _overall_status's zero-user-story headline below
    story_link = _linkage_breakdown(linked_cases, _is_story_linked, latest_run_id)
    code_coverage = _linkage_breakdown(linked_cases, lambda c: bool(c.get("covers")), latest_run_id)
    # Same breakdown, broken out by created_via -- a blended number can hide
    # one origin dragging the other down (a legacy imported suite often has
    # different real coverage than freshly generated cases), and this is
    # also what keeps the dashboard useful for a repo with 100s of test
    # cases but zero user stories tracked in TestAtlas at all.
    story_link_by_origin = _linkage_breakdown_by_origin(linked_cases, _is_story_linked, latest_run_id)
    code_coverage_by_origin = _linkage_breakdown_by_origin(linked_cases, lambda c: bool(c.get("covers")), latest_run_id)
    # _overall_status's zero-user-story headline wants a plain number, not the
    # full breakdown -- None (no fresh cases) reads as 0% there, since the
    # headline already says explicitly how many cases are on file.
    code_coverage_pct = code_coverage["pct"] if code_coverage["pct"] is not None else 0.0

    test_case_evidence = _evidence_summary(test_cases, latest_run_id, reviews_by_target)
    requirement_evidence = _evidence_summary(requirements, latest_run_id, reviews_by_target, use_ai_snapshot=True)
    total_stale = test_case_evidence["stale"] + requirement_evidence["stale"]
    human_confirmed_verified = sum(1 for d in document_rollup if d["health"] == "verified" and d["has_human_confirmed"])

    return {
        "evidence_schema_version": 2,
        "overall": _overall_status(
            health_counts, total_docs, verified_pct, critical_broken, total_cases, code_coverage_pct, total_stale,
            human_confirmed_verified,
        ),
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
            "by_ai_health": ai_health_counts,
            "verified_pct": verified_pct,
            "ai_verified_pct": ai_verified_pct,
            "critical_at_risk": critical_at_risk,
            "by_origin": _count_by(documents, "created_via"),
        },
        "requirements": {
            "total": total_reqs,
            "by_status": req_status,
            "ai_unavailable": ai_unavailable_reqs,
            "contradiction_rate": _pct(req_status["contradicted"], fresh_assessed_reqs),
            "supported_rate": _pct(req_status["supported"], fresh_assessed_reqs),
            "correctness_rate": _pct(req_status["supported"], coded_reqs),
            "correctness_rate_denominator": coded_reqs,
            "evidence": requirement_evidence,
        },
        "test_cases": {
            "total": len(test_cases),
            "by_category": _count_by(test_cases, "category"),
            "by_priority": _count_by(test_cases, "priority"),
            "by_origin": _count_by(test_cases, "created_via"),
            "story_link": story_link,
            "code_coverage": code_coverage,
            "story_link_by_origin": story_link_by_origin,
            "code_coverage_by_origin": code_coverage_by_origin,
            "evidence": test_case_evidence,
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
