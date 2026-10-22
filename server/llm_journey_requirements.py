"""Derives candidate story requirements directly from a journey's real code,
for the case Story Coverage alone can't handle: no user story exists yet.
Mirrors server/llm_story_coverage.py's house style exactly -- one combined
LLM call, citation-validated against kg.doc_gaps.canonical_cover, an uncited
supported/contradicted claim demoted to "unknown" rather than trusted -- with
one new case this session's design added: a pattern the model found but can't
resolve intent on (the brief's own example: "cancellation after dispatch --
bug or feature?") becomes status="needs_human_input" with a short yes/no
question, instead of a guess dressed up as a verdict. That's a third thing,
distinct from "unknown" (insufficient evidence to tell at all): the evidence
here is clear enough to point at, just not enough to judge without someone
who knows the intended business behavior.

Unlike Story Coverage's judgment pass (which compares against a name-only
context), this one sees REAL source for every function in the journey
(kg.source_lookup.gather_journey_sources) -- there's no existing requirement
text to check names against here, so there's nothing to judge without it.
"""
from __future__ import annotations

import json
import os

from kg.doc_gaps import canonical_cover
from llm_logging import call_and_log
from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features
from server.llm_story_coverage import ALLOWED_REQUIREMENT_TYPES

DEFAULT_MODEL = "gpt-4o-mini"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _build_prompt(journey_name: str, sources: dict[str, str]) -> str:
    source_block = "\n\n".join(f"# {name}\n{src}" for name, src in sources.items())
    return f"""You read the real source code of ONE user journey (a traced end-to-end flow through a codebase, not a single function) and find candidate business requirements implied by it -- validation checks, permission checks, and state transitions -- that a written user story usually covers but none exists here yet.

JOURNEY: "{journey_name}"

REAL SOURCE CODE for every function reachable in this journey:
---
{source_block}
---

For each validation check, permission check, or state transition you find:
- Phrase it as a requirement: "positive" (the system MUST do this) or "negative" (the system MUST NOT do this).
- Judge it against the code you were just given:
  - "supported": the code clearly does what the requirement describes, and it's clearly intentional.
  - "contradicted": the code clearly does the OPPOSITE of what a reasonable requirement would be -- a likely bug.
  - "needs_human_input": you found a real pattern (a check, a guard, a transition) but genuinely cannot tell from the code alone whether the behavior is correct or a bug -- give a specific, answerable yes/no "question" a business person could answer (e.g. "The code allows cancellation after an order has shipped. Should that be allowed?"). Do not use this as a lazy default -- only when the ambiguity is real.
  - "unknown": the evidence doesn't clearly support any of the above either way.
- "covers" must list ONLY real function/class/method names from the source above that support your judgment -- never invent one. If you can't point to a real name, you have nothing to report for this pattern.
- Give a one-sentence "rationale" for every judgment (for needs_human_input, briefly explain what you found before asking).

Respond with ONLY a JSON object of this exact shape, no other text:
{{"requirements": [{{"text": "...", "type": "positive"|"negative", "status": "supported"|"contradicted"|"unknown"|"needs_human_input", "covers": ["Name"], "rationale": "...", "question": "..." }}]}}
Omit "question" entirely unless status is "needs_human_input". If nothing checkable is found, respond with {{"requirements": []}}."""


def _check_derived_requirement(r, canon: dict[str, str]) -> tuple[dict | None, str | None]:
    if not isinstance(r, dict):
        return None, "not a JSON object"
    text = _text(r.get("text"))
    if not text:
        return None, "no text"
    rtype = r.get("type")
    if rtype not in ALLOWED_REQUIREMENT_TYPES:
        return None, f"unknown type {rtype!r}"
    status = r.get("status")
    if status not in ("supported", "contradicted", "unknown", "needs_human_input"):
        return None, f"unknown status {status!r}"

    covers: list[str] = []
    for entry in (r.get("covers") or []):
        real = canonical_cover(entry, canon) if isinstance(entry, str) else None
        if real and real not in covers:
            covers.append(real)

    # Nothing to point at -- whether the model meant to assert or to ask,
    # there's no citation underneath it, so it's demoted to the honest "unknown".
    if status in ("supported", "contradicted", "needs_human_input") and not covers:
        status = "unknown"

    question = (_text(r.get("question")) or None) if status == "needs_human_input" else None
    if status == "needs_human_input" and not question:
        status = "unknown"  # a question-less "needs_human_input" is nothing to ask

    return {
        "text": text, "type": rtype, "status": status,
        "covers": covers[:8], "rationale": _text(r.get("rationale")) or None,
        "question": question,
    }, None


def generate_journey_requirements(journey_name: str, sources: dict[str, str], model: str | None = None) -> list[dict]:
    """Calls OpenAI ONCE to find and judge candidate requirements in a
    journey's real source. Raises RuntimeError, message safe to show the
    user, on a missing key, API error, or malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    if not sources:
        return []
    model = model or os.environ.get("OPENAI_JOURNEY_REQUIREMENTS_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    prompt = _build_prompt(journey_name, sources)
    try:
        response = call_and_log("journey_requirements.derive", model, lambda: OpenAI().chat.completions.create(
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

    requirements = parsed.get("requirements", [])
    if not isinstance(requirements, list):
        raise RuntimeError("Model's response had an unexpected shape (requirements wasn't a list).")

    canon = {name: name for name in sources}
    out = []
    for r in requirements:
        checked, _why = _check_derived_requirement(r, canon)
        if checked is not None:
            out.append(checked)
    return out
