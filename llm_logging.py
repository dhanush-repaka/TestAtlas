"""Structured latency + token-usage logging for every OpenAI call this app
makes. Lives at the repo root, not under kg/ or server/ -- kg/journeys.py and
kg/embeddings.py make calls too, and kg/ never imports from server/ (the
one-directional dependency rule established with kg/journeys.py's own
_is_configured() -- see its comment), so a shared logger can't live under
server/ either without breaking that.

One JSON line per call, written to data/llm_calls.log (append-only, same
DATA_ROOT convention as server/db.py's DATA_DIR) and echoed to stdout via the
standard logging module -- so a local run's actual latency and
prompt/completion token usage is visible without hand-instrumenting each of
the ~10 call sites with ad-hoc prints, and without needing the server
console's scrollback (the file survives a restart).

This never changes what a call site does or how it handles failure -- it
wraps the exact same call, logs one line, and re-raises any exception
completely unchanged so the caller's own try/except (every call site already
has one, converting SDK/network errors into a RuntimeError the UI can show)
keeps working exactly as before.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

# OpenAI's reasoning models (o-series, gpt-5+, gpt-6+) use a different sampling
# contract than the older chat models: `reasoning_effort` instead of `temperature`
# (a non-default temperature is a 400 -- only the model's own default, 1, is
# accepted) and `max_completion_tokens` has a real per-model ceiling that a
# request above it also 400s on, rather than silently clamping. Confirmed
# empirically against the real API (gpt-6-luna vs gpt-4o-mini), not inferred from
# docs alone -- OpenAI's own reasoning-model guide doesn't spell out the
# Chat-Completions-specific parameter differences.
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")

# Real max-completion-tokens ceiling per model, used so a token budget is computed
# from the model actually in play rather than a number tuned for gpt-4o-mini --
# which would either waste a bigger model's real ceiling or 400 if the
# OPENAI_TEST_MODEL/OPENAI_REQUIREMENT_TEST_MODEL override falls back to a
# smaller one. Any model not listed falls back to gpt-4o-mini/gpt-4o's own
# ceiling, the safe assumption for an override this app doesn't know about yet.
_MODEL_OUTPUT_CEILINGS = {"gpt-6-luna": 128_000}
_DEFAULT_OUTPUT_CEILING = 16_384


def is_reasoning_model(model: str) -> bool:
    return model.startswith(_REASONING_MODEL_PREFIXES)


def model_output_ceiling(model: str) -> int:
    return _MODEL_OUTPUT_CEILINGS.get(model, _DEFAULT_OUTPUT_CEILING)


def sampling_kwargs(model: str, *, temperature: float = 0, reasoning_effort: str = "none") -> dict:
    """The extra chat.completions.create kwargs this model's family needs, beyond
    messages/response_format/max_completion_tokens. `reasoning_effort` defaults to
    "none": against this app's actual test-generation prompts (not a toy "list as
    many as you can" prompt -- that's misleading here, see below), "none" through
    "high" came back with the SAME case count, so "none" is cheaper and faster for
    identical output quality, not chosen for a count difference that isn't real.
    (A toy, unconstrained enumeration prompt DID show effort hurting count sharply
    -- 96 scenarios at "none" vs 52-66 at "low"/"medium"/"high" -- but this app's
    real prompts carry heavy "don't pad, stay in scope" instructions that this
    model follows much more strictly than gpt-4o-mini regardless of effort, so
    that toy result doesn't transfer and isn't the reason for this default.)"""
    if is_reasoning_model(model):
        return {"reasoning_effort": reasoning_effort}
    return {"temperature": temperature}

# Story Coverage's judge calls now run concurrently (a bounded thread pool,
# see server/llm_story_coverage.py) -- several threads can call _emit() at
# once, and a bare file append isn't safe against interleaved writes.
_write_lock = threading.Lock()

_ROOT = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent)))
_LOG_PATH = _ROOT / "data" / "llm_calls.log"

_logger = logging.getLogger("testatlas.llm")
if not _logger.handlers:  # guard against duplicate handlers if this module is re-imported (uvicorn --reload)
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False


def _usage_dict(response) -> dict | None:
    """Both chat.completions and embeddings responses carry a `.usage` with
    at least prompt_tokens/total_tokens; chat completions also carry
    completion_tokens. Missing attributes are reported as None rather than
    guessed at."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def call_and_log(site: str, model: str, fn: Callable[[], T], *, prompt_chars: int | None = None) -> T:
    """Runs `fn` -- an OpenAI SDK call, e.g. `lambda: client.chat.completions.create(...)`
    -- and logs one line with latency_ms and token usage regardless of
    outcome. `site` identifies which feature made the call (e.g.
    "story_coverage.judge", "journey_requirements.derive") so latency/usage
    can be compared across features. `prompt_chars` records the prompt's raw
    character count as a cheap cross-check alongside the API's own reported
    prompt_tokens (a character count is not a token count, but it's free and
    catches a caller passing a much bigger prompt than it meant to).

    Never swallows an exception -- always re-raised after logging, unchanged,
    so a call site's own try/except (every one has one) behaves exactly as
    it did before this wrapper existed."""
    started = time.monotonic()
    entry: dict = {"site": site, "model": model, "prompt_chars": prompt_chars}
    try:
        response = fn()
    except Exception as e:  # noqa: BLE001 -- log the failure, then let it propagate unchanged
        entry.update(ok=False, error=str(e), latency_ms=round((time.monotonic() - started) * 1000))
        _emit(entry)
        raise
    entry.update(ok=True, latency_ms=round((time.monotonic() - started) * 1000), usage=_usage_dict(response))
    _emit(entry)
    return response


def _emit(entry: dict) -> None:
    if os.environ.get("TESTATLAS_DISABLE_LLM_LOG"):  # set by tests/__init__.py -- never log a mocked call
        return
    entry.setdefault("ts", time.time())
    line = json.dumps(entry, default=str)
    _logger.info(line)
    try:
        with _write_lock:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass  # logging must never break the actual feature


def read_recent(limit: int = 200) -> list[dict]:
    """The last `limit` log entries, newest first -- used by the /api/llm-calls
    route. Tolerates a missing or partially-written file (a line that fails
    to parse is skipped, not fatal)."""
    if not _LOG_PATH.exists():
        return []
    lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
