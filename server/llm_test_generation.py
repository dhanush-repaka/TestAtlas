"""LLM-generated FUNCTIONAL test cases, in Azure DevOps test-case shape, via a
live OpenAI API call.

By explicit user choice, this reuses the same paid, opt-in exception as
automatic gap analysis (server/llm_gap_analysis.py) rather than the free
"build context, hand it to an LLM session" pattern everything else in this
project follows -- gated entirely behind OPENAI_API_KEY being set. Unlike
gap analysis, this feature has no free manual fallback today: with no key
configured, the module list simply doesn't appear.

Functional, not unit. The first version asked for "test cases" from a list of
function and method names, and got what that input invites: one case per
function ("encode an empty string"). A functional test is a scenario a user --
or, for a library, a consumer of its public interface -- would run end to end,
touching several code units at once. So the prompt asks for that explicitly,
and the case shape is Azure DevOps's: title, description, priority,
preconditions, and a Steps grid where EVERY step has an action and its own
expected result. For UI code, kg/ts_parser.py supplies the real on-screen
text (button labels, headings, placeholders, messages) so steps can quote what
the screen actually says instead of inventing wording.

Reuses kg.doc_gaps.module_test_context() for the input -- one call per
MODULE, not per repo: one call over a whole large repo spread a single
~output-token budget across every module and left most with nothing. Unlike
gap analysis, documents are optional (module_test_context always builds with
require_docs=False): code alone is enough, documents just sharpen what counts
as critical and what the intended behavior is.

Output is a structured, reviewable test case -- not runnable code. The model
sees names, purposes and UI text, never function bodies, so it's inferring
plausible behavior; treat the output as a first draft for a tester to review
and adapt. Its `covers` list (which real code units a scenario exercises) IS
checked: names that aren't in the module are dropped rather than trusted.
"""
from __future__ import annotations

import json

from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features

ALLOWED_TEST_CASE_CATEGORIES = {"happy_path", "edge_case", "error_handling"}

DEFAULT_MODEL = "gpt-4o-mini"

# gpt-4o-mini's own output ceiling is 16384 tokens. A functional case -- title,
# description, preconditions and 3-8 steps each with an action AND an expected
# result -- runs ~520 tokens (the earlier unit-style case, with one flat
# expected result, was ~230); ~1500 tokens go to the model echoing/reasoning
# over the prompt. Asking for more than fits truncates the response mid-JSON
# (a real failure hit earlier in this project), so the cap is derived, not round.
_PROMPT_OVERHEAD_TOKENS = 1500
_TOKENS_PER_CASE = 520
_MODEL_TOKEN_CEILING = 16384
HARD_CASE_CAP = (_MODEL_TOKEN_CEILING - _PROMPT_OVERHEAD_TOKENS) // _TOKENS_PER_CASE - 3  # 3-case safety margin

MIN_STEPS = 2  # a one-step "scenario" is an assertion, not a functional test


def _is_test_file(dotted_path: str) -> bool:
    """Heuristic: a file already under a tests/ package or named test_*/*_test
    (Python), or *.test.ts / *.spec.ts / under __tests__ (TypeScript/JavaScript --
    kg/ts_parser.py turns `Button.test.tsx` into the segment `Button_test`), is
    existing test code, not something to write new tests against."""
    segments = dotted_path.lower().split(".")
    return any(
        seg in ("test", "tests", "spec", "specs", "__tests__")
        or seg.startswith("test_") or seg.endswith(("_test", "_spec"))
        for seg in segments
    )


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
    Roughly 0.4 cases per real unit: a functional scenario exercises several
    units at once (add-to-cart touches the button, the action and the cart
    total), so the count grows much more slowly than the unit-test version's
    1.5 per unit. Floored at 3 (a tiny module still deserves a happy path and
    a couple of failure cases) and capped by what one response can hold."""
    real_units = 0
    for m in context["modules"]:
        for f in m["files"]:
            if _is_test_file(f["file"]):
                continue
            real_units += len(f["functions"])
            for c in f["classes"]:
                real_units += 1 + len(c["methods"])
    return max(3, min(HARD_CASE_CAP, round(real_units * 0.4)))


def _build_prompt(context: dict, target: int) -> str:
    module_name = context["modules"][0]["module"] if context["modules"] else "(unknown)"
    display = context.get("display_name")
    named = f' (known as "{display}")' if display else ""
    modules_json = json.dumps(context["modules"], indent=2)
    has_docs = bool(context["documents"])
    docs_block = (
        "\n\n".join(f'DOCUMENT ("{d["name"]}"):\n---\n{d["content"]}\n---' for d in context["documents"])
        if has_docs
        else "(No project documents were provided for this repo. Base every test case on the module's "
             "contents below alone -- infer what the feature is for from its names, purposes, UI text and "
             "file paths.)"
    )
    priority_hint = (
        "the flows the documents describe as important"
        if has_docs
        else "the flows the module's names and UI text suggest are core"
    )
    return f"""You are a QA analyst writing FUNCTIONAL test cases, in the style of Azure DevOps Test Plans, for ONE MODULE of a larger codebase -- module "{module_name}"{named}. Other modules are handled by separate calls like this one.

{docs_block}

THIS MODULE'S ACTUAL CONTENTS -- its files, each file's real top-level functions, its real classes WITH their real method names, and, for user-interface code, the "ui_text" found in each file: the real labels, headings, placeholders and messages a user sees on screen. File paths are ground truth too (for a web app they usually reveal the page route). This is derived directly from the source, not a summary; you do NOT have function/method bodies:
---
{modules_json}
---

WHAT "FUNCTIONAL" MEANS HERE
A functional test case verifies that a feature works from the OUTSIDE, the way a user -- or, for a library/API/service with no UI, a developer consuming its public interface -- would exercise it: a scenario with a goal, run start to finish. It is NOT a unit test. Never write one case per function or method, never "call X with input Y and check it returns Z", never assertions about internals. A good case exercises several of the code units above together as one flow (for example: add a product to the cart, open the cart, change the quantity, remove the item).
- If the module has a user interface: write from the user's side of the screen -- pages, buttons, forms, messages. Where "ui_text" is listed, those are the REAL strings on screen: quote them verbatim in your steps (click "Add To Cart"). Do NOT invent label or message text that isn't listed; when you need to refer to something whose exact wording you weren't given, describe it instead ("the search field", "an error message saying the email is invalid").
- If the module has no UI (a library, API or service): write from the consumer's side of its public interface -- what they call or send, and the observable result or response.
- If the module has no user- or consumer-visible behavior of its own (pure types, config or constants, internal plumbing, or it is itself test code such as a `tests` package or `*.test`/`*.spec` files), respond with {{"test_cases": []}}. Do not force scenarios out of internals.

TEST CASE FORMAT (an Azure DevOps test case)
- "title": an imperative sentence naming the scenario, starting "Verify that ...".
- "description": 1-2 sentences -- what this case verifies and why it matters.
- "priority": 1 = critical path / core business flow, 2 = important, 3 = less common, 4 = rare or cosmetic.
- "category": "happy_path" (normal successful use), "edge_case" (a boundary or unusual-but-valid condition: empty state, limit, repeated action), or "error_handling" (invalid input, refused or failed operation).
- "feature": the feature or user flow under test, as a short noun phrase.
- "preconditions": the state that must hold before step 1 (signed-in user, item already in the cart, ...), or "" if none.
- "steps": {MIN_STEPS} to 8 ordered steps. Each step is {{"action": ..., "expected": ...}}: "action" is ONE concrete thing the tester does; "expected" is what they observe right after THAT action -- concrete and checkable (what appears, changes, is returned or is refused). EVERY step has its own expected result, and the last step's expected result states the overall outcome of the scenario.
- "covers": the real names from the contents above (functions, classes, or `Class.method`) that this scenario exercises, so it can be traced back to code. Use ONLY names that appear above.

COVERAGE
Design approximately {target} test cases. Spread them across the module's genuinely distinct features -- don't pile several cases on one feature and skip another. For each major feature write a happy-path case, then the failure and boundary cases a real tester would run: invalid or missing input, empty states (empty cart, empty list, no results), limits, repeated or duplicate actions, expired or invalid sessions or tokens, unauthorized access, an unavailable dependency. Express every one of them as a user action with an observable outcome, never an internal check. Give priority to {priority_hint}.

GROUNDING
Base everything on the names, purposes, UI text and documents above. Because you can't see function bodies, don't assert exact numbers, formats or messages you weren't given -- describe outcomes at the level you can actually support. Don't invent features the contents don't suggest.

Respond with ONLY a JSON object of this exact shape, no other text:
{{"test_cases": [
  {{
    "title": "Verify that ...",
    "description": "...",
    "priority": 2,
    "category": "happy_path" | "edge_case" | "error_handling",
    "feature": "...",
    "preconditions": "...",
    "steps": [{{"action": "...", "expected": "..."}}, {{"action": "...", "expected": "..."}}],
    "covers": ["Name", "Class.method"]
  }},
  ...
]}}

If this module and the documents genuinely don't support meaningful functional test cases, respond with {{"test_cases": []}}."""


# --------------------------------------------------------------------------- validating the model's output

_ACTION_KEYS = ("action", "step", "instruction", "description")
_EXPECTED_KEYS = ("expected", "expected_result", "expected_outcome", "result")


def _known_code_names(context: dict) -> dict[str, str]:
    """Every name the model may legitimately cite in `covers` -> its canonical
    form. Bare method names map to `Class.method` (first class wins)."""
    canon: dict[str, str] = {}
    for m in context["modules"]:
        for f in m["files"]:
            for fn in f["functions"]:
                canon.setdefault(fn, fn)
            for c in f["classes"]:
                canon.setdefault(c["name"], c["name"])
                for meth in c["methods"]:
                    canon.setdefault(f'{c["name"]}.{meth}', f'{c["name"]}.{meth}')
                    canon.setdefault(meth, f'{c["name"]}.{meth}')
    return canon


def _canonical_cover(entry: str, canon: dict[str, str]) -> str | None:
    """Maps what the model wrote to a real name, tolerating a module-path
    prefix (`src.lib.Signer.sign` -> `Signer.sign`); None means it's invented."""
    e = entry.strip()
    if e in canon:
        return canon[e]
    parts = e.replace(":", ".").split(".")
    for k in (2, 1):
        if len(parts) >= k and ".".join(parts[-k:]) in canon:
            return canon[".".join(parts[-k:])]
    return None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        t = _text(d.get(k))
        if t:
            return t
    return ""


def _normalize_case(c, canon: dict[str, str]) -> dict | None:
    """One raw model case -> a validated case, or None if it isn't a usable
    functional test case (missing title/steps, a step with no expected result,
    fewer than MIN_STEPS steps). Tolerates the key spellings models drift
    between; never invents content to fill a gap."""
    if not isinstance(c, dict):
        return None
    title = _text(c.get("title"))
    category = c.get("category")
    if not title or category not in ALLOWED_TEST_CASE_CATEGORIES or not isinstance(c.get("steps"), list):
        return None

    steps = []
    for raw in c["steps"]:
        if not isinstance(raw, dict):
            return None  # a bare string step has no expected result of its own -- not the format asked for
        action, expected = _first_text(raw, _ACTION_KEYS), _first_text(raw, _EXPECTED_KEYS)
        if not action or not expected:
            return None
        steps.append({"action": action, "expected": expected})
    if len(steps) < MIN_STEPS:
        return None

    try:
        priority = max(1, min(4, int(c.get("priority", 2))))
    except (TypeError, ValueError):
        priority = 2

    covers: list[str] = []
    raw_covers = c.get("covers")
    for entry in raw_covers if isinstance(raw_covers, list) else []:
        real = _canonical_cover(entry, canon) if isinstance(entry, str) else None
        if real and real not in covers:
            covers.append(real)

    return {
        "title": title,
        "description": _text(c.get("description")) or None,
        "category": category,
        "priority": priority,
        "target": _text(c.get("feature")) or None,      # the feature / flow under test
        "preconditions": _text(c.get("preconditions")) or None,
        "steps": steps,
        "expected_result": steps[-1]["expected"],        # overall outcome = the last step's (the DB column predates per-step results)
        "covers": covers[:8],
        "edge_case_description": None,                   # superseded by per-step expected results
    }



def run_test_generation(context: dict, model: str = DEFAULT_MODEL) -> list[dict]:
    """Calls the OpenAI API to design functional test cases from `context`
    (from kg.doc_gaps.module_test_context, scoped to one module) and returns a
    validated list of {title, description, category, priority, target (the
    feature under test), preconditions, steps: [{action, expected}],
    expected_result, covers}. Raises RuntimeError with a message safe to show
    the user on any failure -- missing key, API error, or a malformed
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
            temperature=0.3,  # a little variety helps cover more distinct scenarios than temperature=0
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

    canon = _known_code_names(context)
    validated = []
    for c in cases[:HARD_CASE_CAP]:
        normalized = _normalize_case(c, canon)
        if normalized:
            validated.append(normalized)
    return validated
