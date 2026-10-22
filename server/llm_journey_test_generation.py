"""Generates ONE Azure DevOps-shaped, multi-step end-to-end test case directly
FROM a journey's already-judged member requirements (server/llm_story_coverage.py
for document-sourced ones, server/llm_journey_requirements.py for code-derived
ones) -- the "login -> cart -> checkout -> payment" test case that neither
module-at-a-time generation (server/llm_test_generation.py) nor one-
requirement-at-a-time generation (server/llm_requirement_test_generation.py)
can structurally produce, since neither sees more than one piece of the flow
at once.

Same inherit-never-re-judge rule as llm_requirement_test_generation.py,
generalized from one requirement to a journey's whole member list: the model
writes exactly one step per already-judged requirement, in flow order: never
re-judges any of them. A journey with one contradicted member must show that
at the STEP level -- the overall case status is the worst status among its
members (contradicted > needs_human_input > unknown > supported), never a
single fake "mostly fine" verdict that would hide the one bad step.
"""
from __future__ import annotations

import json
import os

from llm_logging import call_and_log
from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features
from server.llm_test_generation import _check_case

DEFAULT_MODEL = "gpt-4o-mini"

_STATUS_SEVERITY = {"contradicted": 3, "needs_human_input": 2, "unknown": 1, "supported": 0}


def _build_prompt(journey_name: str, requirements: list[dict], sources: dict[str, str]) -> str:
    req_lines = [
        f"{i}. [{r['status'].upper()}] ({r['type']}) {r['text']} -- cites: {', '.join(r.get('covers') or []) or '(none)'}"
        for i, r in enumerate(requirements, 1)
    ]
    sources_block = ""
    if sources:
        snippets = "\n\n".join(f"# {name}\n{src}" for name, src in sources.items())
        sources_block = f"\n\nREAL SOURCE CODE for the names these requirements cite:\n---\n{snippets}\n---"
    return f"""You write ONE Azure DevOps-style functional test case narrating an end-to-end USER JOURNEY -- a real multi-step flow, not one isolated check. Each numbered requirement below is ALREADY judged -- do not re-judge any of them. Write exactly ONE test step per requirement, IN THE SAME ORDER given, so step N narrates requirement N.

JOURNEY: "{journey_name}"

ALREADY-JUDGED MEMBER REQUIREMENTS, in flow order:
{chr(10).join(req_lines)}{sources_block}

For each requirement above, write one step narrating it as a user would experience it: {{"action": "...", "expected": "..."}}. For a requirement marked CONTRADICTED, the step's expected result must describe the INTENDED (spec) behavior the requirement claims, not the actual buggy behavior -- the inherited status is what flags the mismatch, the step just documents the spec. For NEEDS_HUMAN_INPUT or UNKNOWN, still narrate the expected behavior as the requirement states it, phrased plainly, without resolving the ambiguity yourself.

Give the overall case a title, category ("happy_path", "edge_case", or "error_handling" -- pick "error_handling" if any member is contradicted or negative), priority (1 = critical path .. 4 = rare), and optional preconditions.

Respond with ONLY a JSON object of this exact shape, no other text, with EXACTLY {len(requirements)} steps in order:
{{"test_case": {{"title": "...", "category": "happy_path"|"edge_case"|"error_handling", "priority": 1, "preconditions": "...", "steps": [{{"action": "...", "expected": "..."}}], "description": "..."}}}}"""


def generate_test_case_from_journey(
    journey_name: str, requirements: list[dict], sources: dict[str, str], model: str | None = None,
) -> dict:
    """Calls the OpenAI API ONCE to narrate a journey's member requirements as
    one sequential ADO-shape test case. Raises RuntimeError, message safe to
    show the user, on a missing key, no requirements, API error, malformed
    response, or a result _check_case rejects. The returned case's
    `covers`/`status`/`rationale` are always derived from the requirements'
    own judgments, never the model's -- the model's narration is validated
    and used, its implicit judgment (if any) is discarded. Each step is
    tagged with the requirement text and status that backs it, matched
    positionally (one step was asked per requirement, in the same order)."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    if not requirements:
        raise RuntimeError("this journey has no judged requirements yet -- nothing to generate from")
    model = model or os.environ.get("OPENAI_JOURNEY_TEST_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    prompt = _build_prompt(journey_name, requirements, sources)
    try:
        response = call_and_log("journey_test_generation.generate", model, lambda: OpenAI().chat.completions.create(
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

    raw_case = parsed.get("test_case")
    if not isinstance(raw_case, dict):
        raise RuntimeError("Model's response had an unexpected shape (test_case wasn't a JSON object).")

    # canon here only needs to let _check_case's own covers-handling pass --
    # the result is overwritten with the requirements' own covers right after.
    canon = {name: name for r in requirements for name in (r.get("covers") or [])}
    checked, why = _check_case(raw_case, canon, ui=False)
    if checked is None:
        raise RuntimeError(f"Model's test case was rejected: {why}")

    # Steps are matched POSITIONALLY to member requirements (one step was
    # asked per requirement, in order) -- never trust the model's own count
    # if it drifted; a step past the given requirements is left untagged
    # rather than mis-attributed to the wrong one.
    tagged_steps = []
    for i, step in enumerate(checked["steps"]):
        req = requirements[i] if i < len(requirements) else None
        tagged_steps.append({
            **step,
            "requirement_text": req["text"] if req else None,
            "status": req["status"] if req else None,
        })
    checked["steps"] = tagged_steps

    checked["covers"] = sorted({name for r in requirements for name in (r.get("covers") or [])})
    checked["status"] = max((r["status"] for r in requirements), key=lambda s: _STATUS_SEVERITY.get(s, 0))
    checked["rationale"] = "; ".join(f"{r['text']}: {r.get('rationale') or r['status']}" for r in requirements)
    return checked
