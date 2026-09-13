"""Friendly, business-English module names via a live OpenAI API call.

Same paid, opt-in exception as server/llm_gap_analysis.py and
server/llm_test_generation.py -- gated entirely behind OPENAI_API_KEY. This
one is deliberately the cheapest of the three: it labels every module in a
repo with ONE call (not one per module), since naming needs far less context
per module than gap analysis or test-case design does.

The technical module name (the `domain` grouping kg/dev_graph_builder.py
computes -- a dotted package path like "kg" or "src.itsdangerous.signer") is
never replaced. It's the stable key everything else in this app already
keys off of (module_test_context's `module` query param, doc_gaps'
per-module grouping, stored test cases' `module` column, Compare Runs).
Friendly names are a separate, purely cosmetic label stored in
server/db.py's module_labels table -- keyed by (repo_id, module), so unlike
per-run enrichment they survive a re-run untouched: the same dotted path
just keeps whatever friendly name it was last given, no matter how many
times "Run analysis" is clicked afterward.
"""
from __future__ import annotations

import json

from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all three features

DEFAULT_MODEL = "gpt-4o-mini"

# Naming needs far less signal per module than gap analysis/test generation --
# a handful of representative real names plus a purpose summary (if any file
# has one) is enough to infer what a module is for. Capping this keeps the
# one-call-for-every-module prompt compact even on a 200+ module repo.
_MAX_SAMPLE_NAMES_PER_MODULE = 8


def _summarize_modules(modules: list[dict]) -> list[dict]:
    """Trims doc_gaps.gap_analysis_context()'s full per-file/class/method
    structure down to one compact summary per module -- real names are
    still real (never invented), just not exhaustively listed."""
    summaries = []
    for m in modules:
        names: list[str] = []
        purpose = None
        for f in m["files"]:
            if purpose is None and f.get("purpose"):
                purpose = f["purpose"]
            names.extend(f["functions"])
            names.extend(c["name"] for c in f["classes"])
            if len(names) >= _MAX_SAMPLE_NAMES_PER_MODULE:
                break
        summaries.append({
            "module": m["module"],
            "sample_names": names[:_MAX_SAMPLE_NAMES_PER_MODULE],
            "purpose": purpose,
        })
    return summaries


def _build_prompt(modules: list[dict]) -> str:
    summary_json = json.dumps(_summarize_modules(modules), indent=2)
    return f"""You name modules in a codebase for a non-technical audience -- a product manager or business stakeholder who has never read the code and doesn't know what "kg" or "src.itsdangerous.signer" means.

Each module below is a dotted package path (its real, technical grouping -- do not change or explain this path, you're only naming it) plus a few real class/function names from inside it and a purpose summary where one exists:
---
{summary_json}
---

For each module, write a short (2-5 word) business-friendly display name that describes what it actually DOES, inferred from its real names and purpose -- not a cosmetic reformatting of the path itself (e.g. turning "kg" into "Kg Module" is not acceptable; infer real meaning). Two different modules can get similar-sounding names if they really do similar things -- don't force artificial distinctiveness.

Respond with ONLY a JSON object of this exact shape, no other text, with one entry per module listed above (same "module" key, exactly as given):
{{"labels": {{"<module>": "<Friendly Name>", ...}}}}"""


def generate_module_names(context: dict, model: str = DEFAULT_MODEL) -> dict[str, str]:
    """Calls the OpenAI API to name every module in `context["modules"]`
    (from kg.doc_gaps.gap_analysis_context) and returns a validated
    {module: friendly_name} dict, restricted to modules actually present in
    the input. Raises RuntimeError with a message safe to show the user on
    any failure -- missing key, API error, or a malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")

    modules = context.get("modules") or []
    if not modules:
        return {}
    known_modules = {m["module"] for m in modules}

    from openai import OpenAI  # lazy import: only needed when this is actually called

    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _build_prompt(modules)}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e

    labels = parsed.get("labels", {})
    if not isinstance(labels, dict):
        raise RuntimeError("Model's response had an unexpected shape (labels wasn't an object).")

    return {
        module: name.strip()
        for module, name in labels.items()
        if module in known_modules and isinstance(name, str) and name.strip()
    }
