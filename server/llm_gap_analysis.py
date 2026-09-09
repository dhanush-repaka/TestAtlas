"""Automatic gap analysis via a live OpenAI API call.

This is deliberately the ONE part of TestAtlas that costs money per use --
by explicit user choice. Every other LLM-shaped feature in this project
(purpose-summary enrichment, the manual gap-analysis flow) avoids a metered
API call on principle; this endpoint is the sole, opt-in exception, gated
entirely behind OPENAI_API_KEY being set. With no key configured, this
module is a no-op and the manual "Get analysis context" + paste-findings
flow (kg/doc_gaps.py) still works exactly as before, for free.
"""
from __future__ import annotations

import json
import os

from kg.doc_gaps import ALLOWED_GAP_CATEGORIES

DEFAULT_MODEL = "gpt-4o-mini"


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _build_prompt(context: dict) -> str:
    modules_json = json.dumps(context["modules"], indent=2)
    return f"""You compare a project document against the ACTUAL contents of a codebase to find genuine, concrete gaps -- not writing-style feedback.

DOCUMENT ("{context['document']['name']}"):
---
{context['document']['content']}
---

ACTUAL CODEBASE CONTENTS -- every module, its files, and each file's real classes/functions (this is ground truth, derived directly from the source via AST parsing, not a summary):
---
{modules_json}
---

Compare the document's claims against what the codebase actually contains. Only report a finding when you have concrete evidence for it in the data above -- do not speculate or invent function/class names that aren't listed. For each real gap, classify it as exactly one of:
- "missing_implementation": the document claims a capability that does not appear anywhere in the listed code
- "undocumented_capability": the code contains significant functionality (a whole module, a notable class/function) that the document never mentions
- "mismatch": both the document and the code address the same thing, but disagree on a concrete detail (scope, behavior, naming)

Respond with ONLY a JSON object of this exact shape, no other text:
{{"findings": [{{"category": "missing_implementation", "description": "..."}}, ...]}}

If you find no genuine gaps, respond with {{"findings": []}}."""


def run_gap_analysis(context: dict, model: str = DEFAULT_MODEL) -> list[dict]:
    """Calls the OpenAI API to compare `context` (from kg.doc_gaps.gap_analysis_context)
    and returns a validated list of {category, description} findings. Raises
    RuntimeError with a message safe to show the user on any failure --
    missing key, API error, or a malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")

    from openai import OpenAI  # lazy import: only needed when this is actually called

    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _build_prompt(context)}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    findings = parsed.get("findings", [])
    if not isinstance(findings, list):
        raise RuntimeError("Model's response had an unexpected shape (findings wasn't a list).")

    validated = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        category, description = f.get("category"), f.get("description")
        if category in ALLOWED_GAP_CATEGORIES and isinstance(description, str) and description.strip():
            validated.append({"category": category, "description": description.strip()})
    return validated
