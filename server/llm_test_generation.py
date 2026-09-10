"""LLM-generated test cases via a live OpenAI API call.

By explicit user choice, this reuses the same paid, opt-in exception as
automatic gap analysis (server/llm_gap_analysis.py) rather than the free
"build context, hand it to an LLM session" pattern everything else in this
project follows -- gated entirely behind OPENAI_API_KEY being set. Unlike
gap analysis, this feature has no free manual fallback today: with no key
configured, the "Generate test cases" button simply doesn't appear.

Reuses kg.doc_gaps.module_test_context() for the input -- one call per
MODULE, not per repo. An earlier version ran one call over the whole
codebase; on a large repo (hundreds of modules) that one call's ~59-case
output-token budget got spread across every module, leaving most of them
with zero cases. Scoping each call to a single module gives that module its
own full budget, so "many test cases per module" becomes achievable
regardless of how many modules the repo has -- at the cost of one call per
module generated, same metered-and-opt-in principle as everything else
here. Unlike gap analysis, documents are optional (module_test_context
always builds with require_docs=False): code structure alone is enough to
generate test cases, documents just sharpen what counts as "critical" and
what the intended behavior is when present.

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

# gpt-4o-mini's own output ceiling is 16384 tokens. ~230 tokens/case (title,
# target, preconditions, several steps, expected result, edge case, plus
# JSON overhead) was measured against real output; ~1500 tokens go to the
# model echoing/reasoning over the prompt. So the real achievable ceiling is
# ~64 cases, not an arbitrary round number -- asking for more than this would
# just truncate the response mid-JSON (a real failure hit while building
# this: an earlier flat max_tokens=8000 truncated a 65-case response).
_PROMPT_OVERHEAD_TOKENS = 1500
_TOKENS_PER_CASE = 230
_MODEL_TOKEN_CEILING = 16384
HARD_CASE_CAP = (_MODEL_TOKEN_CEILING - _PROMPT_OVERHEAD_TOKENS) // _TOKENS_PER_CASE - 5  # 5-case safety margin


def _is_test_file(dotted_path: str) -> bool:
    """Heuristic: a file already under a tests/ package or named test_*/  \
    *_test is existing test code, not something to write new tests against."""
    segments = dotted_path.lower().split(".")
    return any(seg in ("test", "tests") or seg.startswith("test_") or seg.endswith("_test") for seg in segments)


def _target_case_count(context: dict) -> int:
    """Scales the requested case count with the actual testable surface --
    real (non-test) functions, classes, AND each class's real methods
    (kg.doc_gaps.gap_analysis_context includes method names now; a class
    used to count as a flat "1" regardless of how many methods it actually
    has, which badly undercounted any class-heavy codebase's true testable
    surface -- a class contributes 1 (itself, e.g. construction/exceptions)
    plus one per real method, not just 1) -- rather than a flat number, so a
    small module doesn't get padded with filler and a large one doesn't get
    capped down to a token count that was only ever right for a small one.
    Aims for roughly 1-2 cases per real unit (happy path + edge/error mix),
    bounded so a huge module still fits in one call at reasonable cost. The
    floor is deliberately low (3, not the 15 an earlier whole-repo version
    used) -- context is now one module at a time, and a tiny module (a
    couple of helper functions) genuinely doesn't need 15 cases."""
    real_units = 0
    for m in context["modules"]:
        for f in m["files"]:
            if _is_test_file(f["file"]):
                continue
            real_units += len(f["functions"])
            for c in f["classes"]:
                real_units += 1 + len(c["methods"])
    return max(3, min(HARD_CASE_CAP, round(real_units * 1.5)))


def _build_prompt(context: dict, target: int) -> str:
    module_name = context["modules"][0]["module"] if context["modules"] else "(unknown)"
    modules_json = json.dumps(context["modules"], indent=2)
    has_docs = bool(context["documents"])
    docs_block = (
        "\n\n".join(f'DOCUMENT ("{d["name"]}"):\n---\n{d["content"]}\n---' for d in context["documents"])
        if has_docs
        else "(No project documents were provided for this repo. Base every test case on the module's "
             "contents below alone -- infer intended behavior from naming, purpose summaries, class/function "
             "shape, and ordinary conventions for this kind of code.)"
    )
    priority_1 = (
        "The most business-critical flows described in the documents that this module implements."
        if has_docs
        else "The most structurally important code in this module: classes/functions that many others "
             "depend on or that define the module's main public surface, and names suggesting core "
             "business logic (e.g. auth, payment, signing, validation) over incidental helpers."
    )
    priority_3 = (
        "At least one happy-path case per major documented flow this module implements, so edge cases "
        "have a baseline to contrast with."
        if has_docs
        else "At least one happy-path case per real method or function you target, so edge cases have a "
             "baseline to contrast with."
    )
    return f"""You design test cases for ONE MODULE of a larger codebase -- module "{module_name}" -- using its documentation (when available) and its real structure as evidence for what it should do and what surface actually exists to test. You are NOT being asked to cover the whole codebase, only this module; other modules are handled by separate calls like this one.

{docs_block}

THIS MODULE'S ACTUAL CONTENTS -- its files, each file's real top-level functions, and each file's real classes WITH their real method names (this is ground truth, derived directly from the source via AST parsing, not a summary; you do NOT have the function/method bodies, only names/purposes, so infer plausible behavior from naming, purpose summaries, and the documents above rather than inventing internal logic):
---
{modules_json}
---

If this module is itself the codebase's own test code (e.g. a `tests` package, files named `test_*`), don't write tests of those tests -- respond with {{"test_cases": []}}.

Design approximately {target} test cases -- that number reflects the actual size of this module's real (non-test) surface (every top-level function, every class, and every one of its real methods), so treat it as a real target, not a suggestion to undershoot: aim for roughly 1-2 cases per meaningfully distinct function or method (a happy path, plus an edge/error case where one genuinely applies), not one case for the whole module -- and prefer targeting a class's actual listed methods (e.g. `Signer.unsign`) over the class as a whole wherever real methods are listed. It's fine to land a bit under or over if the module genuinely warrants it, but don't stop early out of caution -- a real method you haven't covered yet is a real gap, not a reason to stop. Prioritize:
1. {priority_1}
2. Realistic edge cases for each: boundary values (empty/zero/max/min), invalid or malformed input, missing/null fields, error and exception paths, expiry/timeout conditions, permission/auth failures, and concurrent or repeated use where relevant.
3. {priority_3}

Do not invent function/class/method names that aren't listed above. Each test case must be traceable to something real: {"a documented flow, and/or " if has_docs else ""}an actual class, method, or top-level function from this module's contents.

For each test case, classify it as exactly one of:
- "happy_path": exercises normal, expected usage (per the documents when available, otherwise per what the code's naming/shape implies it's meant to do)
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

If this module and the documents genuinely don't support meaningful test cases, respond with {{"test_cases": []}}."""


def run_test_generation(context: dict, model: str = DEFAULT_MODEL) -> list[dict]:
    """Calls the OpenAI API to design test cases from `context` (from
    kg.doc_gaps.module_test_context, scoped to one module) and returns a
    validated list of {title, category, target, preconditions, steps,
    expected_result, edge_case_description} dicts. Raises RuntimeError with a message safe to
    show the user on any failure -- missing key, API error, or a malformed
    response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")

    from openai import OpenAI  # lazy import: only needed when this is actually called

    target = _target_case_count(context)
    # Scale the token budget with the target count -- a fixed budget that was
    # only ever right for a small repo silently truncates a larger repo's
    # response mid-JSON (see HARD_CASE_CAP's derivation above for why).
    max_tokens = min(_MODEL_TOKEN_CEILING, _PROMPT_OVERHEAD_TOKENS + target * _TOKENS_PER_CASE)

    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _build_prompt(context, target)}],
            response_format={"type": "json_object"},
            temperature=0.2,  # a little variety helps cover more distinct edge cases than temperature=0
            max_tokens=max_tokens,
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
