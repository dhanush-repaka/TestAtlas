"""Codifies, as plain functions rather than left implicit in a prompt, how
confident the dashboard is allowed to sound about an AI-judged test case or
story requirement. Pure computation, no LLM calls, same style as
server/traceability_dashboard.py -- reads what llm_test_verification.py /
llm_story_coverage.py / llm_gap_analysis.py already wrote, never re-judges
anything itself.

Two independent signals, surfaced side by side rather than one overwriting
the other:
  evidence_level   what the AI's own citation-backed judgment says -- an
                    item with no real code citation can never be anything
                    but "insufficient_evidence", regardless of what status
                    string it carries (mirrors the demotion
                    server/llm_test_verification.py's _check_verification
                    already enforces at write time; this just makes it an
                    explicit, queryable label instead of leaving it implicit
                    in "status").
  human review      a server/db.py human_reviews row, if a person has looked
                    at this specific item -- a DIFFERENT evidence type, never
                    silently merged into evidence_level. latest_human_decision
                    below looks this up; callers show both together.

Staleness is the third, orthogonal signal: whether this item's own
`run_id` predates the repo's current latest successful analysis run. A
repo that re-analyzes often will mark items stale often -- that's
intentional, not a bug: "this was judged against code that has since
changed in TestAtlas's view" is exactly the risk the item is flagging.
"""
from __future__ import annotations

EVIDENCE_LEVELS = ("insufficient_evidence", "inconclusive", "ai_supported", "ai_conflict")

EVIDENCE_LEVEL_LABELS = {
    "insufficient_evidence": "Insufficient evidence",
    "inconclusive": "Inconclusive",
    "ai_supported": "AI-supported",
    "ai_conflict": "Potential conflict",
}

# Returned by ai_evidence_level (never by evidence_level) for a story_requirement
# written before ai_status/ai_covers existed -- there is no frozen AI snapshot to
# read at all, which is a different fact from "the AI found no citations"
# (insufficient_evidence) and must never be silently treated as either evidence
# level or reconstructed by reading the row's current, possibly human-confirmed
# status/covers.
AI_ASSESSMENT_UNAVAILABLE = "ai_assessment_unavailable"

# What a human reviewer can record against a test case or story requirement
# (server/db.py's human_reviews.decision) -- distinct from an AI status string,
# since a reviewer is allowed to say "I can't tell either" without that being
# read as agreement with the AI.
HUMAN_REVIEW_DECISIONS = (
    "confirmed_supported", "confirmed_contradicted", "agreed_uncertain", "rejected_ai_judgment", "needs_more_info",
)


def evidence_level(item: dict) -> str:
    """`item` is a test_cases or story_requirements row (server/db.py's
    list_test_cases / list_story_requirements shape): needs `covers` (list)
    and `status` (one of supported/contradicted/unknown/needs_human_input/None).
    Never trusts a bare status string over the citations backing it -- an
    item with zero real citations is "insufficient_evidence" even if some
    upstream bug ever left a stray status on it."""
    covers = item.get("covers") or []
    status = item.get("status")
    if not covers:
        return "insufficient_evidence"
    if status == "supported":
        return "ai_supported"
    if status == "contradicted":
        return "ai_conflict"
    return "inconclusive"  # unknown, needs_human_input, or any other/missing status


def ai_evidence_level(item: dict) -> str:
    """Like evidence_level, but strictly the AI's OWN last judgment, immune to
    anything a human has since done to the row. For a story_requirement (which
    carries a separate ai_status/ai_covers snapshot -- server/db.py's
    _STORY_REQUIREMENTS_COLUMN_MIGRATIONS -- because confirm_story_requirement
    overwrites status/covers in place), reads ai_status/ai_covers, NEVER
    status/covers. For anything without an `ai_status` key at all (a test
    case: its status/covers are never overwritten by human review in the
    first place, see update_test_case_verification's docstring, so the live
    fields already ARE the AI's own judgment), falls back to evidence_level.
    Returns AI_ASSESSMENT_UNAVAILABLE for a story_requirement written before
    ai_status existed (ai_status is None) -- this must never be guessed at
    by falling back to the row's current, possibly human-confirmed status."""
    if "ai_status" not in item:
        return evidence_level(item)
    if item.get("ai_status") is None:
        return AI_ASSESSMENT_UNAVAILABLE
    return evidence_level({"status": item.get("ai_status"), "covers": item.get("ai_covers") or []})


def effective_ai_status(item: dict) -> str | None:
    """The plain status string ai_evidence_level derives its verdict from --
    `None` only for a legacy story_requirement with no ai_status snapshot.
    Used wherever a caller needs the raw status (e.g. a by-status tally)
    rather than the collapsed supported/conflict/inconclusive verdict."""
    if "ai_status" not in item:
        return item.get("status")
    return item.get("ai_status")


def is_stale(item: dict, latest_run_id: str | None) -> bool:
    """True when `item` was computed against an older analysis run than the
    repo's current one. `latest_run_id=None` (no successful run yet, or a
    run-independent item like a human-confirmed row with no run_id) is never
    stale -- there's nothing newer to be stale against."""
    if item.get("is_stale"):
        return True
    if not latest_run_id:
        return False
    return bool(item.get("run_id")) and item["run_id"] != latest_run_id


def latest_review_by_target(reviews: list[dict]) -> dict[str, dict]:
    """`reviews` -- server/db.py's list_human_reviews(repo_id) output, newest
    first. Collapses a target reviewed more than once down to its most
    recent review (a correction history's current verdict), keyed by
    target_id. A caller that also needs target_type should check the
    returned row's own `target_type` field, since target ids are UUIDs and
    collision across the two target types isn't possible in practice but
    isn't asserted here either."""
    out: dict[str, dict] = {}
    for r in reviews:
        out.setdefault(r["target_id"], r)
    return out
