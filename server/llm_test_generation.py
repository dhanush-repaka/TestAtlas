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
import os
import re
from dataclasses import dataclass, field

from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features

ALLOWED_TEST_CASE_CATEGORIES = {"happy_path", "edge_case", "error_handling"}

DEFAULT_MODEL = "gpt-4o-mini"


def _model(override: str | None = None) -> str:
    """OPENAI_TEST_MODEL (a plain env var, not a secret) picks the model without a code
    change. gpt-4o-mini is the default because it costs roughly an order of magnitude
    less per token -- but it's the weaker instruction-follower, so if functional test
    quality matters more than cost, switch this one feature to a stronger model
    (`fly secrets set OPENAI_TEST_MODEL=gpt-4o`)."""
    return override or os.environ.get("OPENAI_TEST_MODEL") or DEFAULT_MODEL

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

# A functional case needs at least one step with its own expected result. This was 2 at first
# ("a one-step scenario is an assertion, not a test") -- and that was WRONG: negative cases are
# naturally short (one action triggers one error message), so the rule deleted exactly the cases
# the module most needed. Found only after discards were made visible: gpt-4o wrote 12 cases for
# one module and 10 were thrown away for having a single step.
MIN_STEPS = 1


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


def _build_prompt(context: dict, target: int, must_trigger: list[str] | None = None) -> str:
    module_name = context["modules"][0]["module"] if context["modules"] else "(unknown)"
    display = context.get("display_name")
    named = f' (known as "{display}")' if display else ""
    about = f'\nWhat this module is for, in the words of the product team: {context["description"]}' if context.get("description") else ""
    modules_json = json.dumps(context["modules"], indent=2)
    has_docs = bool(context["documents"])
    docs_block = (
        "\n\n".join(f'DOCUMENT ("{d["name"]}"):\n---\n{d["content"]}\n---' for d in context["documents"])
        if has_docs
        else "(No project documents were provided for this repo. Base every test case on the module's "
             "contents below alone -- infer what the feature is for from its names, purposes, UI text and "
             "file paths.)"
    )
    must_block = ""
    if must_trigger:
        listed = "\n".join(f'  - "{m}"' for m in must_trigger)
        must_block = f"""
MESSAGES THAT MUST BE TRIGGERED
The code can show each of these on screen -- they are error, validation, empty-state or unavailable messages, and they are what negative test cases exist to reach. Write at least one test case per message whose steps cause it and whose expected result shows that exact text ("error_handling" or "edge_case"):
{listed}
"""
    priority_hint = (
        "the flows the documents describe as important"
        if has_docs
        else "the flows the module's names and UI text suggest are core"
    )
    return f"""You are a QA analyst writing FUNCTIONAL test cases, in the style of Azure DevOps Test Plans, for ONE MODULE of a larger codebase -- module "{module_name}"{named}.{about} Other modules are handled by separate calls like this one.

{docs_block}

THIS MODULE'S ACTUAL CONTENTS -- its files, each file's real top-level functions, its real classes WITH their real method names, and, for user-interface code, the "ui_text" found in each file: the real labels, headings, placeholders and messages a user sees on screen. File paths are ground truth too (for a web app they usually reveal the page route). This is derived directly from the source, not a summary; you do NOT have function/method bodies:
---
{modules_json}
---

WHAT "FUNCTIONAL" MEANS HERE
A functional test case verifies that a feature works from the OUTSIDE, the way a user -- or, for a library/API/service with no UI, a developer consuming its public interface -- would exercise it: a scenario with a goal, run start to finish. It is NOT a unit test. Never write one case per function or method, never "call X with input Y and check it returns Z", never assertions about internals. A good case exercises several of the code units above together as one flow (for example: add a product to the cart, open the cart, change the quantity, remove the item).
- If the module has a user interface: write from the user's side of the screen -- pages, buttons, forms, messages. Where "ui_text" is listed, those are the REAL strings on screen: quote them verbatim in your steps (click "Add To Cart"). Do NOT invent label or message text that isn't listed; when you need to refer to something whose exact wording you weren't given, describe it instead ("the checkout button", "the search field", "an error message saying the email is invalid").
- Do not assume the UI offers an action in a state where it plausibly doesn't. If ui_text shows an empty-state message ("Your cart is empty.") and no control for the action you're imagining, the expected result for that state is the message itself -- not an error triggered by a button that likely isn't there.
- If the module has no UI (a library, API or service): write from the consumer's side of its public interface -- what they call or send, and the observable result or response.
- If the module has no user- or consumer-visible behavior of its own (pure types, config or constants, internal plumbing, or it is itself test code such as a `tests` package or `*.test`/`*.spec` files), respond with {{"test_cases": []}}. Do not force scenarios out of internals.

TEST CASE FORMAT (an Azure DevOps test case)
- "title": an imperative sentence naming the scenario, starting "Verify that ...".
- "description": 1-2 sentences -- what this case verifies and why it matters.
- "priority": 1 = critical path / core business flow, 2 = important, 3 = less common, 4 = rare or cosmetic.
- "category": "happy_path" (normal successful use), "edge_case" (a boundary or unusual-but-valid condition: empty state, limit, repeated action), or "error_handling" (invalid input, refused or failed operation).
- "feature": the feature or user flow under test, as a short noun phrase.
- "preconditions": the state that must hold before step 1 (signed-in user, item already in the cart, ...), or "" if none.
- "steps": ordered steps, up to 8. A happy-path case walks the whole flow from where the user starts to the visible result -- typically 3 to 6 steps (reach the screen, perform the action, confirm the outcome, check any knock-on effect such as a total or a count) -- not a single click. A negative or edge case is usually shorter: one action that triggers an error or empty-state message may be a single step. Never pad a case with filler steps to look longer. Each step is {{"action": ..., "expected": ...}}: "action" is ONE concrete thing the tester does; "expected" is what they observe right after THAT action -- concrete and checkable (what appears, changes, is returned or is refused). EVERY step has its own expected result, and the last step's expected result states the overall outcome of the scenario.
- "covers": the real names from the contents above (functions, classes, or `Class.method`) that this scenario exercises, so it can be traced back to code. Use ONLY names that appear above.

COVERAGE
Design approximately {target} test cases. Spread them across the module's genuinely distinct features -- don't pile several cases on one feature and skip another. For each major feature write a happy-path case, then the failure and boundary cases a real tester would run: invalid or missing input, empty states (empty cart, empty list, no results), limits, repeated or duplicate actions, expired or invalid sessions or tokens, unauthorized access, an unavailable dependency. Express every one of them as a user action with an observable outcome, never an internal check. Give priority to {priority_hint}.
Two rules on top of that:
1. Negative and edge cases matter as much as happy paths -- aim for at least a third of your cases to be "edge_case" or "error_handling", and don't leave a message the UI can show (see "ui_text") untested in favor of more happy paths.
2. Every case is a distinct scenario. Don't write the same flow twice under different wording (for instance "update quantity" and "edit quantity"), and keep implementation details -- cookies, tokens, caches, function names -- out of titles and steps unless the user or consumer can actually see them.

{must_block}
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


def _check_case(c, canon: dict[str, str]) -> tuple[dict | None, str | None]:
    """One raw model case -> (validated case, None), or (None, why it was
    rejected). A usable functional case needs a title, a known category, and at
    least MIN_STEPS steps that EACH have an action and their own expected
    result. Tolerates the key spellings models drift between; never invents
    content to fill a gap -- and never drops one without saying why (an earlier
    version discarded silently, which hid paid model output being thrown away)."""
    if not isinstance(c, dict):
        return None, "not a JSON object"
    title = _text(c.get("title"))
    if not title:
        return None, "no title"
    if c.get("category") is None:
        return None, "no category"
    if c.get("category") not in ALLOWED_TEST_CASE_CATEGORIES:
        return None, f'unknown category {c.get("category")!r}'
    if not isinstance(c.get("steps"), list):
        return None, "steps weren't a list"

    steps = []
    for n, raw in enumerate(c["steps"], 1):
        if not isinstance(raw, dict):
            return None, f"step {n} was plain text, not an action/expected pair"
        action, expected = _first_text(raw, _ACTION_KEYS), _first_text(raw, _EXPECTED_KEYS)
        if not action or not expected:
            return None, f"step {n} was missing its {'action' if not action else 'expected result'}"
        steps.append({"action": action, "expected": expected})
    if len(steps) < MIN_STEPS:
        return None, "no steps"

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
        "category": c["category"],
        "priority": priority,
        "target": _text(c.get("feature")) or None,      # the feature / flow under test
        "preconditions": _text(c.get("preconditions")) or None,
        "steps": steps,
        "expected_result": steps[-1]["expected"],        # overall outcome = the last step's (the DB column predates per-step results)
        "covers": covers[:8],
        "edge_case_description": None,                   # superseded by per-step expected results
    }, None


def _normalize_case(c, canon: dict[str, str]) -> dict | None:
    return _check_case(c, canon)[0]


# --------------------------------------------------------------------------- checked coverage of on-screen messages

# ui_text strings that read like an error / validation / empty-state / unavailable
# message. The code can show each one, so a negative test should reach it.
_MESSAGE_PATTERN = re.compile(
    r"out of stock|sold out|unavailable|not available|\bno (results|items|products|matches|data)\b|\bempty\b|not found|"
    r"\berror\b|invalid|\brequired\b|\bfail(ed|ure)?\b|try again|please (select|enter|choose|provide|sign|log)|\bmust\b|"
    r"cannot|can't|couldn't|could not|unable to|expired|denied|forbidden|not allowed|\blimit\b|maximum|minimum|"
    r"too (long|short|many|few)|\bmissing\b|incorrect|\bwrong\b|something went wrong",
    re.I,
)
_MAX_MUST_TRIGGER = 8


def _must_trigger_messages(context: dict) -> list[str]:
    out: list[str] = []
    for m in context["modules"]:
        for f in m["files"]:
            if _is_test_file(f["file"]):
                continue
            for text in f.get("ui_text", []):
                if _MESSAGE_PATTERN.search(text) and text not in out:
                    out.append(text)
    return out[:_MAX_MUST_TRIGGER]


def _uncovered(messages: list[str], cases: list[dict]) -> list[str]:
    """Messages no case's steps mention -- checked in the text, not trusted from the model's say-so."""
    blob = json.dumps([c["steps"] for c in cases]).lower()
    return [m for m in messages if m.lower().rstrip(".") not in blob]


def _build_repair_prompt(context: dict, missing: list[str], existing_titles: list[str], must_trigger: list[str]) -> str:
    listed = "\n".join(f'  - "{m}"' for m in missing)
    have = "\n".join(f"  - {t}" for t in existing_titles) or "  (none)"
    return _build_prompt(context, len(missing), must_trigger) + f"""

FOLLOW-UP PASS -- this overrides the count and coverage guidance above.
A first pass already wrote test cases with these titles (do NOT repeat or rephrase any of them):
{have}
Those cases never reach the on-screen messages below. Write exactly one NEW test case for each message: its steps must cause the message to appear, and the expected result of the step where it appears must contain that exact text. Use "error_handling" or "edge_case". Respond in the same JSON shape, with ONLY these new cases:
{listed}"""


# --------------------------------------------------------------------------- the model calls

@dataclass
class GenerationResult:
    cases: list[dict]
    dropped: list[str] = field(default_factory=list)          # why each discarded model case was rejected
    repaired: int = 0                                          # cases added by the follow-up coverage pass
    still_uncovered: list[str] = field(default_factory=list)   # on-screen messages no case reaches even after it


def _ask(prompt: str, max_tokens: int, model: str) -> list:
    from openai import OpenAI  # lazy import: only needed when this is actually called

    try:
        response = OpenAI().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
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
    return cases


def generate_test_cases(context: dict, model: str | None = None) -> GenerationResult:
    """Designs functional test cases for `context` (from
    kg.doc_gaps.module_test_context, scoped to one module). Validated cases
    come back with a reason for every discard. If the module's UI can show
    error/empty-state/unavailable messages that no case reaches, ONE follow-up
    call asks only for those (checked in the text, not taken on the model's
    word). Raises RuntimeError, message safe to show the user, on a missing
    key, API error, or malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    model = _model(model)

    target = _target_case_count(context)
    must = _must_trigger_messages(context)
    # Scale the token budget with the target count -- a fixed budget only ever right for a
    # small module silently truncates a larger one mid-JSON (see HARD_CASE_CAP's derivation).
    max_tokens = min(_MODEL_TOKEN_CEILING, _PROMPT_OVERHEAD_TOKENS + target * _TOKENS_PER_CASE)

    canon = _known_code_names(context)
    result = GenerationResult(cases=[])

    def take(raw_cases: list) -> None:
        seen = {c["title"].lower() for c in result.cases}
        for raw in raw_cases[:HARD_CASE_CAP]:
            case, why = _check_case(raw, canon)
            if case is None:
                result.dropped.append(why or "invalid")
            elif case["title"].lower() not in seen and len(result.cases) < HARD_CASE_CAP:
                result.cases.append(case)
                seen.add(case["title"].lower())

    take(_ask(_build_prompt(context, target, must), max_tokens, model))

    missing = _uncovered(must, result.cases)
    if missing and len(result.cases) < HARD_CASE_CAP:
        before = len(result.cases)
        prompt = _build_repair_prompt(context, missing, [c["title"] for c in result.cases], must)
        take(_ask(prompt, min(_MODEL_TOKEN_CEILING, _PROMPT_OVERHEAD_TOKENS + len(missing) * _TOKENS_PER_CASE), model))
        result.repaired = len(result.cases) - before
    result.still_uncovered = _uncovered(must, result.cases)
    return result


def run_test_generation(context: dict, model: str | None = None) -> list[dict]:
    """The validated cases from generate_test_cases(), for callers that don't need the report."""
    return generate_test_cases(context, model).cases
