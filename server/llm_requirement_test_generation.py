"""Generates test case(s) directly FROM a user story's own written text --
one acceptance criterion at a time, or a whole document's worth at once --
WITHOUT ever showing the model any code. This is the "write the full,
realistic set a QA engineer would" half of the pipeline; a separate,
document-scoped "Verify against code" pass (server/app.py's
api_verify_test_cases_for_document, reusing server/llm_test_verification.py's
generate_test_case_verifications) judges the already-written cases against
the real codebase afterward and resolves status/covers/module for the first
time. Generation never requires Story Coverage to have run on this document,
and never inherits or re-derives a prior judgment -- grounding is a later,
separate step, not a precondition.

A requirement (or a whole document) can warrant many test cases: "users can
reserve stock" covers a happy path, but also an over-limit attempt, a
zero-stock attempt, and so on -- scenarios the text implies without spelling
out. The model decides however many of these actually warranted -- a simple,
atomic requirement still yields exactly one case; a large, compound one (or a
rich multi-criterion document) may legitimately warrant dozens.
SCENARIO_SAFETY_CAP below is a client-side accept-cap against a pathological
response, never a number communicated to the model as a target or quota.

Every returned case is stored with status=NULL, covers=[], module=NULL --
genuinely unverified, exactly like server/llm_test_generation.py's module-level
code-driven generation already does (see server/db.py's replace_test_cases,
which never sets a status column at all -- that existing precedent is what
this file now matches, instead of the citation-gated shape it used to have).

Same opt-in, metered-cost exception as the other paid LLM features here --
gated on is_configured() from server/llm_gap_analysis.py, the one place that
gate is defined. No manual fallback, matching test generation's own
convention (writing test cases is the same class of action either way, not
something read-a-document-by-hand replaces).
"""
from __future__ import annotations

import json
import os

from llm_logging import call_and_log
from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features
from server.llm_test_generation import _check_case, _MODEL_TOKEN_CEILING

DEFAULT_MODEL = "gpt-4o-mini"
# A safety rail against a pathological response, not a target -- the prompt
# never mentions this number to the model (see module docstring).
SCENARIO_SAFETY_CAP = 30


def _build_prompt(requirement: dict) -> str:
    return f"""You write Azure DevOps-style functional test case(s) directly from ONE acceptance criterion's own wording -- you are NOT shown the codebase, and you must not guess at or assume implementation details. The test case documents what the criterion's own text claims should happen; whether the code actually does that is checked in a separate step later, not your job here.

CRITERION: {requirement['text']}
TYPE: {requirement['type']} ({"the system MUST do this" if requirement['type'] == 'positive' else "the system MUST NOT do this -- a guard or restriction"})

Decide for yourself how many DISTINCT, independently-checkable scenarios this one criterion implies -- there is no target count or quota. Don't just restate the criterion as a single paraphrased case: think like an experienced QA engineer scoping this specific behavior -- the happy path, plus the boundary values, common error conditions, and realistic edge cases a feature like this typically needs, even where the criterion's own wording doesn't spell every one out.

If this criterion involves a NUMERIC RANGE, THRESHOLD, or LIMIT (a percentage, an amount, a count, a size), apply boundary-value analysis explicitly: write a SEPARATE test case for each meaningfully distinct boundary, not one representative case for the whole range -- e.g. the minimum/zero value, a typical mid-range value, the maximum valid value, a value just past the limit, a value far past the limit, and a fractional/decimal value if the domain allows one. These are NOT artificial variations of each other -- real bugs hide at exactly these boundaries (off-by-one at the limit, rounding at a fraction, a sign error at zero), so each one is its own independently-checkable scenario, not padding. "Never pad" means don't invent scenarios that test nothing new -- it does not mean collapsing a whole numeric range down to one case.

A simple, atomic criterion with no real internal structure still yields exactly one case; never invent a scenario that has nothing to do with what this criterion is actually about.

For EACH scenario, write: title, category ("happy_path", "edge_case", or "error_handling"), priority (1 = critical path .. 4 = rare), optional preconditions, and steps as an ordered list of {{"action": ..., "expected": ...}} pairs (at least one step).

Respond with ONLY a JSON object of this exact shape, no other text:
{{"test_cases": [{{"title": "...", "category": "happy_path"|"edge_case"|"error_handling", "priority": 1, "preconditions": "...", "steps": [{{"action": "...", "expected": "..."}}], "description": "..."}}]}}"""


def generate_test_cases_from_requirement(requirement: dict, model: str | None = None) -> list[dict]:
    """Calls the OpenAI API ONCE to propose and narrate as many test cases as
    the model itself judges this ONE acceptance criterion's text warrants --
    ungrounded: no code is shown, and no prior Story Coverage judgment
    (status/covers/rationale) is fed in or inherited. Every returned case's
    `status` is left unset (NULL at storage) and `covers` forced empty (no
    source was ever shown to cite from) -- genuinely unverified until a later
    "Verify against code" pass judges it. Raises RuntimeError, message safe
    to show the user, on a missing key, no criterion text, API error,
    malformed response, or every proposed scenario being rejected."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    if not (requirement.get("text") or "").strip():
        raise RuntimeError("this requirement has no text to generate from")
    model = model or os.environ.get("OPENAI_REQUIREMENT_TEST_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    prompt = _build_prompt(requirement)
    try:
        response = call_and_log("requirement_test_generation.generate", model, lambda: OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_MODEL_TOKEN_CEILING,  # the model may legitimately propose dozens of scenarios -- don't cap it below its own ceiling
        ), prompt_chars=len(prompt))
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    raw_cases = parsed.get("test_cases")
    if not isinstance(raw_cases, list):
        raise RuntimeError("Model's response had an unexpected shape (test_cases wasn't a list).")

    out: list[dict] = []
    reasons: list[str] = []
    for i, raw_case in enumerate(raw_cases[:SCENARIO_SAFETY_CAP], 1):
        # canon={} -- no source was ever shown, so any "covers" the model
        # invents anyway is rejected, guaranteeing covers=[] coming out of
        # generation. status/rationale are never set here either, so they
        # stay NULL at storage (server/db.py's create_test_case), matching
        # module-level code-driven generation's own existing precedent.
        checked, why = _check_case(raw_case, {}, ui=False)
        if checked is None:
            reasons.append(f"scenario {i} rejected: {why}")
            continue
        out.append(checked)

    if not out:
        detail = "; ".join(reasons) if reasons else "no scenarios were returned"
        raise RuntimeError(f"Model's test case(s) were rejected: {detail}")
    return out


def _build_document_prompt(doc: dict) -> str:
    return f"""You write Azure DevOps-style functional test cases covering a WHOLE user story's worth of scenarios, directly from its own written text -- you are NOT shown any code, and you must not guess at or assume implementation details. Narrate only what the story's own wording implies; whether the code actually does any of this is checked in a separate step later, not your job here.

STORY ("{doc['name']}"):
---
{doc['content']}
---

Decide for yourself how many DISTINCT, independently-checkable scenarios this whole story warrants -- there is no target count or quota. Don't just restate each acceptance criterion as one paraphrased case: think like an experienced QA engineer scoping this feature for real -- for each piece of behavior the story implies, also consider the boundary values, common error conditions, and realistic edge cases a feature like this typically needs, even where the story's own wording doesn't spell every one out explicitly.

If a criterion involves a NUMERIC RANGE, THRESHOLD, or LIMIT (a percentage, an amount, a count, a size), apply boundary-value analysis explicitly: write a SEPARATE test case for each meaningfully distinct boundary, not one representative case for the whole range -- e.g. the minimum/zero value, a typical mid-range value, the maximum valid value, a value just past the limit, a value far past the limit, and a fractional/decimal value if the domain allows one. These are NOT artificial variations of each other -- real bugs hide at exactly these boundaries (off-by-one at the limit, rounding at a fraction, a sign error at zero), so each one is its own independently-checkable scenario, not padding.

A simple, narrow story may only need a handful of cases; a well-scoped, meaty story (a full login flow, or a pricing rule with several numeric limits) can easily warrant well over a dozen once you account for its happy path, each distinct edge case, each guard/error condition, and each boundary value a numeric criterion implies. Let the story's own real complexity decide -- "never pad" means don't invent scenarios that test nothing new, not collapsing a whole numeric range down to one case -- and never under-cover a genuinely rich story just to keep the list short.

For EACH scenario, write: title, category ("happy_path", "edge_case", or "error_handling"), priority (1 = critical path .. 4 = rare), optional preconditions, and steps as an ordered list of {{"action": ..., "expected": ...}} pairs (at least one step).

Respond with ONLY a JSON object of this exact shape, no other text:
{{"test_cases": [{{"title": "...", "category": "happy_path"|"edge_case"|"error_handling", "priority": 1, "preconditions": "...", "steps": [{{"action": "...", "expected": "..."}}], "description": "..."}}]}}
If the story has nothing checkable in it, respond with {{"test_cases": []}}."""


def generate_test_cases_from_document(doc: dict, model: str | None = None) -> list[dict]:
    """Like generate_test_cases_from_requirement, scoped to an ENTIRE
    document's own raw text instead of one acceptance criterion -- ungrounded,
    no Story Coverage prerequisite, no code shown. Raises RuntimeError,
    message safe to show the user, on a missing key, empty document content,
    API error, malformed response, or every proposed scenario being rejected."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    if not (doc.get("content") or "").strip():
        raise RuntimeError("this document has no content yet -- nothing to generate from")
    model = model or os.environ.get("OPENAI_REQUIREMENT_TEST_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    prompt = _build_document_prompt(doc)
    try:
        response = call_and_log("requirement_test_generation.generate_for_document", model, lambda: OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_MODEL_TOKEN_CEILING,
        ), prompt_chars=len(prompt))
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    raw_cases = parsed.get("test_cases")
    if not isinstance(raw_cases, list):
        raise RuntimeError("Model's response had an unexpected shape (test_cases wasn't a list).")

    out: list[dict] = []
    reasons: list[str] = []
    for i, raw_case in enumerate(raw_cases[:SCENARIO_SAFETY_CAP], 1):
        checked, why = _check_case(raw_case, {}, ui=False)
        if checked is None:
            reasons.append(f"scenario {i} rejected: {why}")
            continue
        out.append(checked)

    if not out:
        detail = "; ".join(reasons) if reasons else "no scenarios were returned"
        raise RuntimeError(f"Model's test case(s) were rejected: {detail}")
    return out
