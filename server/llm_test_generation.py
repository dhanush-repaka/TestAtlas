"""LLM-generated test cases via a live OpenAI API call.

By explicit user choice, this reuses the same paid, opt-in exception as
automatic gap analysis (server/llm_gap_analysis.py) rather than the free
"build context, hand it to an LLM session" pattern everything else in this
project follows -- gated entirely behind OPENAI_API_KEY being set. Unlike
gap analysis, this feature has no free manual fallback today: with no key
configured, the "Generate test cases" button simply doesn't appear.

Reuses kg.doc_gaps.gap_analysis_context() for the input -- the context a
gap comparison needs (every document + every module's real files/classes/
functions/purpose summaries) is exactly what test-case generation needs too,
just put to a different prompt.

Output is a structured, reviewable QA-style test case (title, preconditions,
steps, expected result, which edge case it targets) -- not runnable code.
The model only ever sees names and purpose summaries, never actual function
bodies (the graph doesn't carry those), so it's inferring plausible behavior
from naming/purpose/doc context, not reading real logic. Treat the output as
a fast first draft to review and adapt, not a QA suite ready to run as-is.
"""
from __future__ import annotations

import json

from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates both features

ALLOWED_TEST_CASE_CATEGORIES = {"happy_path", "edge_case", "error_handling"}

DEFAULT_MODEL = "gpt-4o-mini"

# Hard safety ceiling on one call's cost/output size -- not the practical
# target (see _target_case_count), just a backstop against a pathological
# response.
HARD_CASE_CAP = 200


def _is_test_file(dotted_path: str) -> bool:
    """Heuristic: a file already under a tests/ package or named test_*/  \
    *_test is existing test code, not something to write new tests against."""
    segments = dotted_path.lower().split(".")
    return any(seg in ("test", "tests") or seg.startswith("test_") or seg.endswith("_test") for seg in segments)


def _target_case_count(context: dict) -> int:
    """Scales the requested case count with the actual testable surface --
    real (non-test) classes/functions -- rather than a flat number, so a
    small repo doesn't get padded with filler and a large one doesn't get
    capped down to a token count that was only ever right for a small one.
    Aims for roughly 2-3 cases per real unit (happy path + edge/error mix),
    bounded so a huge repo still fits in one call at reasonable cost."""
    real_units = 0
    for m in context["modules"]:
        for f in m["files"]:
            if _is_test_file(f["file"]):
                continue
            real_units += len(f["classes"]) + len(f["functions"])
    return max(15, min(HARD_CASE_CAP, round(real_units * 2.5)))


def _build_prompt(context: dict) -> str:
    modules_json = json.dumps(context["modules"], indent=2)
    docs_block = "\n\n".join(
        f'DOCUMENT ("{d["name"]}"):\n---\n{d["content"]}\n---' for d in context["documents"]
    )
    target = _target_case_count(context)
    return f"""You design test cases for a codebase, using its documentation and its real structure as evidence for what it should do and what surface actually exists to test.

{docs_block}

ACTUAL CODEBASE CONTENTS -- every module, its files, and each file's real classes/functions (this is ground truth, derived directly from the source via AST parsing, not a summary; you do NOT have the function bodies, only names/purposes, so infer plausible behavior from naming, purpose summaries, and the documents above rather than inventing internal logic):
---
{modules_json}
---

Some of the modules above are the codebase's OWN EXISTING TESTS (e.g. a `tests` package, files named `test_*`). Never target those with a new test case -- there's no value in writing a test of a test. Treat them only as a signal for what's already covered.

Design approximately {target} test cases -- that number reflects the actual size of the real (non-test) surface above, so treat it as a real target, not a suggestion to undershoot: aim for roughly 2-3 cases per meaningfully distinct class/function (one happy path plus 1-2 edge/error cases), not one case per file or one case for the whole module. It's fine to land a bit under or over if the codebase genuinely warrants it, but don't stop early out of caution. Prioritize:
1. The most business-critical flows described in the documents.
2. Realistic edge cases for each: boundary values (empty/zero/max/min), invalid or malformed input, missing/null fields, error and exception paths, expiry/timeout conditions, permission/auth failures, and concurrent or repeated use where relevant to what's documented.
3. At least one happy-path case per major documented flow, so edge cases have a baseline to contrast with.

Do not invent function/class names that aren't listed above. Each test case must be traceable to something real: a documented flow, and/or an actual class or function from the codebase contents (excluding the existing-tests modules, per above).

For each test case, classify it as exactly one of:
- "happy_path": exercises normal, expected, documented usage
- "edge_case": a boundary, unusual, or rarely-hit but valid condition
- "error_handling": an invalid input or failure condition that should be rejected or handled gracefully

Respond with ONLY a JSON object of this exact shape, no other text:
{{"test_cases": [
  {{
    "title": "short descriptive name",
    "category": "happy_path" | "edge_case" | "error_handling",
    "target": "the class/function/flow this exercises, e.g. itsdangerous.signer.Signer.unsign",
    "preconditions": "state required before running this case, or empty string if none",
    "steps": ["step 1", "step 2", "..."],
    "expected_result": "what should happen if the code is correct",
    "edge_case_description": "the specific edge condition this targets, or empty string for happy_path"
  }},
  ...
]}}

If the codebase and documents genuinely don't support meaningful test cases, respond with {{"test_cases": []}}."""


def run_test_generation(context: dict, model: str = DEFAULT_MODEL) -> list[dict]:
    """Calls the OpenAI API to design test cases from `context` (from
    kg.doc_gaps.gap_analysis_context) and returns a validated list of
    {title, category, target, preconditions, steps, expected_result,
    edge_case_description} dicts. Raises RuntimeError with a message safe to
    show the user on any failure -- missing key, API error, or a malformed
    response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")

    from openai import OpenAI  # lazy import: only needed when this is actually called

    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _build_prompt(context)}],
            response_format={"type": "json_object"},
            temperature=0.2,  # a little variety helps cover more distinct edge cases than temperature=0
            max_tokens=8000,  # a larger, size-scaled case count needs real headroom -- a cut-off response
                               # would just fail json.loads below instead of quietly returning fewer cases
        )
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    cases = parsed.get("test_cases", [])
    if not isinstance(cases, list):
        raise RuntimeError("Model's response had an unexpected shape (test_cases wasn't a list).")

    validated = []
    for c in cases[:HARD_CASE_CAP]:
        if not isinstance(c, dict):
            continue
        title = c.get("title")
        category = c.get("category")
        steps = c.get("steps")
        expected_result = c.get("expected_result")
        if not (isinstance(title, str) and title.strip()):
            continue
        if category not in ALLOWED_TEST_CASE_CATEGORIES:
            continue
        if not (isinstance(steps, list) and steps and all(isinstance(s, str) for s in steps)):
            continue
        if not (isinstance(expected_result, str) and expected_result.strip()):
            continue
        validated.append({
            "title": title.strip(),
            "category": category,
            "target": c.get("target") if isinstance(c.get("target"), str) else None,
            "preconditions": c.get("preconditions") if isinstance(c.get("preconditions"), str) else None,
            "steps": [s.strip() for s in steps if s.strip()],
            "expected_result": expected_result.strip(),
            "edge_case_description": (
                c.get("edge_case_description") if isinstance(c.get("edge_case_description"), str) else None
            ),
        })
    return validated
