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

# What a human reviewer can record against a test case or story requirement
# (server/db.py's human_reviews.decision) -- distinct from an AI status string,
# since a reviewer is allowed to say "I can't tell either" without that being
# read as agreement with the AI.
HUMAN_REVIEW_DECISIONS = (
    "confirmed_supported", "confirmed_contradicted", "rejected_ai_judgment", "needs_more_info",
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


def is_stale(item: dict, latest_run_id: str | None) -> bool:
    """True when `item` was computed against an older analysis run than the
    repo's current one. `latest_run_id=None` (no successful run yet, or a
    run-independent item like a human-confirmed row with no run_id) is never
    stale -- there's nothing newer to be stale against."""
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
