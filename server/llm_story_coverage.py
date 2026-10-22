"""Story Coverage: extracts atomic acceptance criteria from ONE user-story
document, then judges each one against real retrieved source -- two LLM
passes, not one combined call. Earlier this dumped every module's name into
one prompt and judged everything at once; that stopped being the right
tradeoff once a repo is large enough that a flat name-dump both dilutes the
model's signal and risks exceeding context. Now: one call decomposes the
document into atomic criteria (no judging yet), then each criterion is
embedded and matched against a per-run module index (kg/embeddings.py) built
from text TestAtlas already generates for other reasons (module
descriptions, UI strings, route paths) -- retrieval narrows the whole
codebase down to the ~10 modules most likely relevant, whose REAL source
(kg.source_lookup.gather_module_sources) is what the one-call-per-criterion
judgment actually sees. An 8-criterion story costs ~9 chat calls plus one
cheap embeddings call, not 1 -- paid in exchange for judgment that holds up
as repos grow, and for real source grounding this pass never had before.

Mirrors server/llm_gap_analysis.py's "one prompt in, one JSON array out"
shape for the decompose step; the judge step is the same per-item,
citation-validated pattern server/llm_requirement_test_generation.py and
server/llm_journey_requirements.py use. Scoped to one document instead of
all of them (this feature is inherently per-story, unlike gap analysis which
compares every document as one corpus against the whole codebase -- see
kg/doc_gaps.py's module docstring for why that one bundles everything
together; that reasoning is about avoiding false "undocumented" findings
across documents, which doesn't apply to "how much of this one story is
implemented").

Same opt-in, metered-cost exception as gap analysis and test generation --
gated entirely behind OPENAI_API_KEY (see is_configured(), imported from
server/llm_gap_analysis.py, the one place this gate is defined). With no key
configured, the "Get coverage context" + paste-requirements manual flow still
works for free.

Citation checking reuses kg.doc_gaps.known_code_names()/canonical_cover() --
the same nominal (name-set, not substring) validation server/llm_test_generation.py
uses for its `covers` field, since no source text ever reaches the graph to
match against -- only names, paths, domain and optional purpose summaries do.
A judgment ("supported" or "contradicted") that cites no real name is demoted
to "unknown": the mechanism that actually enforces "never a bare guess" at
the validation layer, not just as a prompt instruction.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from kg import embeddings
from kg.doc_gaps import known_code_names, canonical_cover
from kg.source_lookup import gather_module_sources
from llm_logging import call_and_log
from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features
from server.llm_journey_test_generation import _STATUS_SEVERITY

ALLOWED_REQUIREMENT_TYPES = {"positive", "negative"}
ALLOWED_REQUIREMENT_STATUSES = {"supported", "contradicted", "unknown"}

DEFAULT_MODEL = "gpt-4o-mini"
# Below this, a document decomposing to zero requirements isn't surprising
# (it may just be a title with nothing checkable in it) -- not worth a retry.
_MIN_CONTENT_FOR_RETRY = 40
# Bounded concurrency for the per-requirement judge calls -- enough to
# meaningfully cut wall-clock on a story with many requirements, conservative
# enough to stay well inside typical OpenAI rate limits.
_JUDGE_CONCURRENCY = 6


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _decompose_prompt(doc: dict) -> str:
    return f"""Break this ONE user story / requirements document into atomic, independently-checkable acceptance criteria. Split any compound sentence into separate criteria. Classify each as:
- "positive": the system MUST do this
- "negative": the system MUST NOT do this (a guard, a restriction, a forbidden state)

Do NOT judge anything against code yet -- that happens in a separate pass. Just decompose.

DOCUMENT ("{doc['name']}"):
---
{doc['content']}
---

Respond with ONLY a JSON object of this exact shape, no other text:
{{"requirements": [{{"text": "...", "type": "positive"|"negative"}}]}}
If the document has no checkable acceptance criteria, respond with {{"requirements": []}}."""


def _judge_prompt(text: str, rtype: str, sources: dict[str, str]) -> str:
    source_block = "\n\n".join(f"# {name}\n{src}" for name, src in sources.items()) or "(no closely-matching code was retrieved)"
    return f"""Judge ONE acceptance criterion from a user story against real source code retrieved as the closest match for it.

CRITERION ({rtype}): {text}

REAL SOURCE CODE -- the modules a retrieval pass judged most likely relevant to this criterion:
---
{source_block}
---

Answer:
- "supported": you can point to specific code above that does what the criterion describes (or, for a negative criterion, a specific guard/check that prevents it)
- "contradicted": the code above does the opposite of what the criterion describes
- "unknown": you cannot tell from the code above alone -- the honest answer when the evidence doesn't decide it, including when nothing relevant was retrieved; do not guess

"covers" must list ONLY real function/class/method names from the code above that support your judgment -- never invent one. If you can't point to a real name, the status must be "unknown". Give a one-sentence "rationale".

Respond with ONLY a JSON object of this exact shape, no other text:
{{"status": "supported"|"contradicted"|"unknown", "covers": ["Name"], "rationale": "..."}}"""


def _check_requirement(r, canon: dict[str, str]) -> tuple[dict | None, str | None]:
    if not isinstance(r, dict):
        return None, "not a JSON object"
    text = _text(r.get("text"))
    if not text:
        return None, "no text"
    rtype = r.get("type")
    if rtype not in ALLOWED_REQUIREMENT_TYPES:
        return None, f"unknown type {rtype!r}"
    status = r.get("status")
    if status not in ALLOWED_REQUIREMENT_STATUSES:
        return None, f"unknown status {status!r}"

    covers: list[str] = []
    for entry in (r.get("covers") or []):
        real = canonical_cover(entry, canon) if isinstance(entry, str) else None
        if real and real not in covers:
            covers.append(real)

    # A "supported"/"contradicted" verdict backed by no real citation is exactly
    # the bare guess this feature exists to rule out -- demote rather than trust it.
    if status in ("supported", "contradicted") and not covers:
        status = "unknown"

    return {
        "text": text, "type": rtype, "status": status,
        "covers": covers[:8], "rationale": _text(r.get("rationale")) or None,
    }, None


def validate_requirements(context: dict, raw: list[dict]) -> list[dict]:
    """Used by the manual/free paste-JSON path (server/app.py) -- same
    per-item checks as the LLM path, so a human-pasted judgment gets the
    identical never-cite-a-fake-name guarantee the paid path gets."""
    canon = known_code_names(context)
    out = []
    for r in raw:
        checked, _why = _check_requirement(r, canon)
        if checked is not None:
            out.append(checked)
    return out


@dataclass
class CoverageResult:
    requirements: list[dict]
    dropped: list[str] = field(default_factory=list)  # why each discarded model item was rejected


def _call_json(client, model: str, prompt: str, site: str = "story_coverage") -> dict:
    try:
        response = call_and_log(site, model, lambda: client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        ), prompt_chars=len(prompt))
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e


def generate_story_requirements(
    doc: dict, context: dict, g, source_root: Path, index_path: Path | None = None, model: str | None = None,
) -> CoverageResult:
    """Decomposes `doc` into acceptance criteria (one call), then judges each
    one (one call per criterion) against real source for the modules a
    retrieval pass over a per-run embedding index judged most relevant --
    narrowing a whole-codebase search down to a short, real-source-backed
    shortlist instead of a flat name dump. `index_path`, when given, caches
    the embedding index alongside the run's graph so it's built once per
    analysis run, not recomputed on every Story Coverage call; a missing or
    unbuildable index falls back to judging against every module (the old
    behavior), never a hard failure. Raises RuntimeError, message safe to
    show the user, on a missing key, API error, or malformed decompose
    response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")
    model = model or os.environ.get("OPENAI_STORY_COVERAGE_MODEL") or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    client = OpenAI()

    def decompose_once() -> list[dict]:
        parsed = _call_json(client, model, _decompose_prompt(doc), site="story_coverage.decompose")
        decomposed = parsed.get("requirements", [])
        if not isinstance(decomposed, list):
            raise RuntimeError("Model's response had an unexpected shape (requirements wasn't a list).")
        out = []
        for r in decomposed:
            if not isinstance(r, dict):
                continue
            text = _text(r.get("text"))
            rtype = r.get("type")
            if text and rtype in ALLOWED_REQUIREMENT_TYPES:
                out.append({"text": text, "type": rtype})
        return out

    atoms = decompose_once()
    # A non-trivial document that decomposed to NOTHING is suspicious (a bulk
    # import's document is, by construction, one real work item -- see
    # server/work_item_import.py) -- retry once before accepting "nothing
    # checkable here" at face value, same "don't trust a single pass" caution
    # applied everywhere else in this module.
    if not atoms and len(doc.get("content", "").strip()) > _MIN_CONTENT_FOR_RETRY:
        atoms = decompose_once()

    result = CoverageResult(requirements=[])
    if not atoms:
        return result

    all_modules = [m["module"] for m in context.get("modules") or []]
    index: list[dict] = []
    try:
        index = embeddings.load_index(index_path) if index_path else []
        if not index:
            index = embeddings.build_module_index(context)
            if index_path:
                embeddings.save_index(index, index_path)
    except RuntimeError:
        index = []  # retrieval is an optimization, not a hard dependency

    query_embeddings: list[list[float] | None]
    if index:
        try:
            query_embeddings = embeddings.embed_texts([a["text"] for a in atoms])
        except RuntimeError:
            query_embeddings = [None] * len(atoms)
    else:
        query_embeddings = [None] * len(atoms)

    canon = known_code_names(context)

    def judge_one(atom: dict, q_emb) -> tuple[dict | None, str | None]:
        """One atom's full judge step -- retrieval lookup, source gathering,
        the judge call, validation. Runs inside the thread pool below; must
        never raise (a thread pool future's exception would otherwise just
        vanish until .result() is called) -- any failure becomes a drop
        reason instead."""
        candidate_modules = embeddings.retrieve_candidate_modules(index, q_emb, k=10) if q_emb is not None else all_modules
        sources = gather_module_sources(g, source_root, candidate_modules)
        try:
            judged = _call_json(client, model, _judge_prompt(atom["text"], atom["type"], sources), site="story_coverage.judge")
        except RuntimeError as e:
            return None, f"judgment call failed for {atom['text']!r}: {e}"
        return _check_requirement({**atom, **judged}, canon)

    # Concurrent, not sequential -- a story with 15-20 requirements used to
    # mean 15-20 judge calls back to back (5-12s each, measured this
    # session); a bounded pool cuts that roughly proportionally. Results
    # come back via executor.map, which preserves input order even though
    # the calls themselves complete out of order.
    with ThreadPoolExecutor(max_workers=min(_JUDGE_CONCURRENCY, len(atoms))) as executor:
        outcomes = list(executor.map(judge_one, atoms, query_embeddings))

    for checked, why in outcomes:
        if checked is None:
            result.dropped.append(why or "invalid")
        else:
            result.requirements.append(checked)
    return result


def compute_tested_status(requirements: list[dict], test_cases: list[dict]) -> list[dict]:
    """'Tested %' is computed at read time, not stored -- intersect each
    requirement's `covers` against the union of this repo's test_cases.covers.
    Cheap, and avoids a new staleness source (test cases and requirements are
    generated independently and either can be regenerated at any time)."""
    covered_by: dict[str, list[str]] = {}
    for c in test_cases:
        for name in c.get("covers") or []:
            covered_by.setdefault(name, []).append(c["title"])
    out = []
    for r in requirements:
        hits = sorted({t for name in (r.get("covers") or []) for t in covered_by.get(name, [])})
        out.append({**r, "tested": bool(hits), "tested_by": hits[:5]})
    return out


def link_requirements_to_cases(cases: list[dict], requirements: list[dict]) -> list[dict]:
    """The reverse of compute_tested_status: for each test case, which story
    requirements (across every document, not just one) cite an overlapping
    code name. Surfaces "why does this test case matter" -- a test case
    whose only linked requirement is itself `contradicted` is a much higher-
    priority thing to look at than one with no business-story link at all.

    For a PURE code-driven case (no source_requirement_id/source_doc_id/
    source_journey_id of its own, so no status from generation either --
    server/llm_test_generation.py's module-level generator is grounded in
    real source now, but deliberately never compares itself against the user
    story), this ALSO promotes the match above from "traceability only" to
    the case's own `status`/`rationale`: a module-driven case was written
    from the real code, so the only honest question left is whether that
    code matches what the story asked for -- and Story Coverage has already
    answered that for any requirement sharing a citation with this case.
    Worst-of-matches (same reduction server/llm_requirement_test_generation.py's
    generate_test_cases_from_document already uses) when more than one
    requirement disagrees. A case matching nothing, or one that already
    carries its own status (story/journey-driven, or already verified), is
    never touched here -- computed fresh at read time, never written back."""
    requirements_by_id = {r["id"]: r for r in requirements}
    requirements_by_name: dict[str, list[dict]] = {}
    for r in requirements:
        for name in r.get("covers") or []:
            requirements_by_name.setdefault(name, []).append(r)
    out = []
    for c in cases:
        seen_ids: set[str] = set()
        matches = []
        matched_requirements = []
        for name in c.get("covers") or []:
            for r in requirements_by_name.get(name, []):
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    matches.append({"doc_id": r["doc_id"], "text": r["text"], "status": r["status"]})
                    matched_requirements.append(r)
        # The AUTHORITATIVE source document -- already stored directly on the
        # row for an ungrounded, story-driven case (server/llm_requirement_test_generation.py),
        # or, for an older requirement-sourced case that predates that column,
        # derived from source_requirement_id's own document. Distinct from
        # `matches` above, which is a best-effort heuristic (shared citations)
        # that can include requirements this case merely happens to overlap
        # with, not its origin.
        source_req = requirements_by_id.get(c.get("source_requirement_id"))
        source_doc_id = c.get("source_doc_id") or (source_req["doc_id"] if source_req else None)

        case_out = {**c, "related_requirements": matches, "source_doc_id": source_doc_id}
        is_code_driven = not c.get("source_requirement_id") and not c.get("source_doc_id") and not c.get("source_journey_id")
        if is_code_driven and not c.get("status") and matched_requirements:
            worst = max(matched_requirements, key=lambda r: _STATUS_SEVERITY.get(r["status"], 0))
            case_out["status"] = worst["status"]
            case_out["rationale"] = worst.get("rationale")
        out.append(case_out)
    return out
