"""A lightweight retrieval index over a repo's modules, so Story Coverage's
per-requirement judgment call (server/llm_story_coverage.py) can narrow its
context to a short, relevant shortlist instead of dumping every module's
contents into every call -- the flat-dump approach works today because this
session's demo repos are small, but stops holding up as a repo grows past
what comfortably fits in one prompt.

Built once per analysis run and cached alongside the run's graph (not
recomputed per Story Coverage call): one OpenAI embeddings call over each
module's friendly name/description, extracted UI strings, and route paths --
text TestAtlas already generates for other reasons, reused here rather than
re-deriving anything new. No vector database -- at the scale of "modules in
one repo" (tens to a few hundred), plain cosine similarity over an in-memory
list is the right amount of engineering, not a premature one.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from llm_logging import call_and_log

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _module_text(module: dict) -> str:
    """One short natural-language blurb per module -- the same clues
    server/llm_module_naming.py already extracts, reused here instead of
    re-deriving anything new."""
    parts = [module.get("module", "")]
    if module.get("description"):
        parts.append(module["description"])
    for f in module.get("files") or []:
        parts.extend(f.get("ui_text") or [])
    routes = module.get("routes") or []
    parts.extend(f"{r.get('method','')} {r.get('path','')}".strip() for r in routes)
    return " ".join(p for p in parts if p)[:2000]  # a cap keeps one pathological module from dominating token cost


def build_module_index(context: dict, model: str | None = None) -> list[dict]:
    """One OpenAI embeddings call over every module in `context["modules"]`
    (from kg.doc_gaps.gap_analysis_context). Returns
    [{"module": <name>, "text": <blurb>, "embedding": [...]}]. Raises
    RuntimeError, message safe to show the user, on a missing key or API
    error -- callers (server/llm_story_coverage.py) fall back to the
    whole-codebase context on failure rather than blocking Story Coverage
    entirely."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    modules = context.get("modules") or []
    if not modules:
        return []
    model = model or os.environ.get("OPENAI_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    texts = [_module_text(m) or m["module"] for m in modules]
    try:
        response = call_and_log("embeddings.build_module_index", model, lambda: OpenAI().embeddings.create(model=model, input=texts),
                                 prompt_chars=sum(len(t) for t in texts))
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI embeddings call failed: {e}") from e

    return [
        {"module": m["module"], "text": t, "embedding": d.embedding}
        for m, t, d in zip(modules, texts, response.data)
    ]


def save_index(index: list[dict], path: Path) -> None:
    path.write_text(json.dumps(index), encoding="utf-8")


def load_index(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def retrieve_candidate_modules(index: list[dict], query_embedding: list[float], k: int = 10) -> list[str]:
    """Top-k module names by cosine similarity to an already-embedded query.
    Embedding the query itself is the caller's job (one call can batch every
    requirement in a story together, cheaper than one call per requirement)."""
    scored = sorted(index, key=lambda e: _cosine(e["embedding"], query_embedding), reverse=True)
    return [e["module"] for e in scored[:k]]


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embeds arbitrary query text (e.g. a decomposed requirement's own
    sentence) -- same model as build_module_index, so the two are comparable."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    if not texts:
        return []
    model = model or os.environ.get("OPENAI_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    try:
        response = call_and_log("embeddings.embed_texts", model, lambda: OpenAI().embeddings.create(model=model, input=texts),
                                 prompt_chars=sum(len(t) for t in texts))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OpenAI embeddings call failed: {e}") from e
    return [d.embedding for d in response.data]
