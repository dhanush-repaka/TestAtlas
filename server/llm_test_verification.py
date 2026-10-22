"""Test Case Verification: judges already-generated test cases (server/llm_test_generation.py's
output, stored in the `test_cases` table) against the codebase's actual contents, one module at
a time, in a single combined LLM call. This is the "trueness" half of the original problem --
Story Coverage (server/llm_story_coverage.py) answers "do we have the right test cases"; this
answers "is a given test case's claimed behavior actually accurate" -- using the exact same
mechanism (citation validation, three-state status, demotion on a fabricated citation) now
proven there, pointed at test cases instead of story requirements.

A test case is matched back by its real DB id, not by title text (which could collide), and a
case the model's response doesn't address at all stays unverified rather than being forced to
"unknown" -- that would overstate what the model actually said.

Same opt-in, metered-cost exception as the other paid LLM features -- gated entirely behind
OPENAI_API_KEY (is_configured(), imported from server/llm_gap_analysis.py, the one place this
gate is defined). Unlike gap analysis and Story Coverage, there is no free manual fallback here,
matching test generation's own convention: judging code is the same class of action as
generating from it, not something a human reads a document and reasons about by hand.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from kg.doc_gaps import known_code_names, canonical_cover
from llm_logging import call_and_log
from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features

ALLOWED_VERIFICATION_STATUSES = {"supported", "contradicted", "unknown"}

DEFAULT_MODEL = "gpt-4o-mini"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _build_prompt(cases: list[dict], context: dict, sources: dict[str, str] | None = None) -> str:
    cases_json = json.dumps([
        {"id": c["id"], "title": c["title"], "steps": c.get("steps") or [],
         "expected_result": c.get("expected_result"), "covers": c.get("covers") or []}
        for c in cases
    ], indent=2)
    modules_json = json.dumps(context["modules"], indent=2)
    sources = sources or {}
    source_block = ""
    if sources:
        snippets = "\n\n".join(f"# {name}\n{src}" for name, src in sources.items())
        source_block = f"""

REAL SOURCE CODE for the functions/methods the test cases cite above (kg/source_lookup.py read these off disk) -- this is what they actually DO, use it to judge behavior, not just that the name exists:
---
{snippets}
---"""
    return f"""You check already-written functional test cases against a codebase's actual contents, to judge whether each one's claimed behavior is genuinely true -- not a style review.

TEST CASES TO VERIFY (matched back by "id"):
---
{cases_json}
---

ACTUAL CODEBASE CONTENTS -- every module, its files, each file's real functions, and each file's real classes WITH their real methods (ground truth, from AST parsing){' -- source for the cited ones follows below' if sources else ' -- you do NOT have function bodies for anything not shown below'}:
---
{modules_json}
---{source_block}

For each test case, judge whether the code actually supports its steps and expected result(s). If real source is given above for a name this case cites, judge from what that code actually does, not from the function's name alone:
- "supported": you can point to real code that does what the steps/expected results describe
- "contradicted": the code does something different from what's claimed
- "unknown": you cannot tell from the names/structure above -- the honest answer when the evidence doesn't decide it; do not guess, and do not assume a function does what its name suggests when you don't have its source

For every judgment, "covers" must list ONLY real function/class/method names from the codebase contents above that support your judgment -- never invent a name that doesn't appear there. If you can't point to a real name, the status must be "unknown", not a guess dressed up as "supported" or "contradicted". Give a one-sentence "rationale". If a test case's claim is genuinely out of scope to judge from the names above, omit it rather than guessing -- an omitted case is left unverified, not marked wrong.

Respond with ONLY a JSON object of this exact shape, no other text:
{{"verifications": [{{"id": "...", "status": "supported"|"contradicted"|"unknown", "covers": ["Name", "Class.method"], "rationale": "..."}}]}}

If none of the test cases can be judged, respond with {{"verifications": []}}."""


def _check_verification(item, valid_ids: set[str], canon: dict[str, str]) -> tuple[dict | None, str | None]:
    if not isinstance(item, dict):
        return None, "not a JSON object"
    case_id = item.get("id")
    if not isinstance(case_id, str) or case_id not in valid_ids:
        return None, f"unknown or missing id {case_id!r}"
    status = item.get("status")
    if status not in ALLOWED_VERIFICATION_STATUSES:
        return None, f"unknown status {status!r}"

    covers: list[str] = []
    for entry in (item.get("covers") or []):
        real = canonical_cover(entry, canon) if isinstance(entry, str) else None
        if real and real not in covers:
            covers.append(real)

    # A "supported"/"contradicted" verdict backed by no real citation is exactly
    # the bare guess this feature exists to rule out -- demote rather than trust it.
    if status in ("supported", "contradicted") and not covers:
        status = "unknown"

    return {
        "id": case_id, "status": status,
        "covers": covers[:8], "rationale": _text(item.get("rationale")) or None,
    }, None


@dataclass
class VerificationResult:
    results: list[dict]
    dropped: list[str] = field(default_factory=list)  # why each discarded model item was rejected


def generate_test_case_verifications(
    cases: list[dict], context: dict, sources: dict[str, str] | None = None, model: str | None = None
) -> VerificationResult:
    """Calls the OpenAI API ONCE to judge `cases` (this module's already-stored
    test cases) against `context` (from kg.doc_gaps.module_test_context) and,
    where available, `sources` (from kg.source_lookup.gather_cited_sources --
    real function bodies for the names these cases cite, not just their
    names). Raises RuntimeError, message safe to show the user, on a missing
    key, API error, or malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    model = model or os.environ.get("OPENAI_TEST_VERIFICATION_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    prompt = _build_prompt(cases, context, sources)
    try:
        response = call_and_log("test_verification.run", model, lambda: OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        ), prompt_chars=len(prompt))
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    verifications = parsed.get("verifications", [])
    if not isinstance(verifications, list):
        raise RuntimeError("Model's response had an unexpected shape (verifications wasn't a list).")

    valid_ids = {c["id"] for c in cases}
    canon = known_code_names(context)
    result = VerificationResult(results=[])
    for item in verifications:
        checked, why = _check_verification(item, valid_ids, canon)
        if checked is None:
            result.dropped.append(why or "invalid")
        else:
            result.results.append(checked)
    return result
