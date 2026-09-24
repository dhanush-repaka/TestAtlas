"""Plain-English names for a repo's modules, for people who have never read
the code -- via a live OpenAI API call.

Same paid, opt-in exception as server/llm_gap_analysis.py and
server/llm_test_generation.py -- gated entirely behind OPENAI_API_KEY.

The audience is a business analyst or product owner. They know the product
from USING it (for a shop: Home Page, Search, Product Page, Shopping Cart,
Checkout, Navigation Bar), not from its folders, so "components.layout.navbar"
and "lib.shopify.fragments" mean nothing to them. An earlier version asked for
a "business-friendly name describing what the module does" and got names like
"Data Encoding Utilities" -- friendlier than the path, but still a developer's
view. So this asks for the SCREEN or FEATURE a module powers, in the words
someone looking at the running product would use, and gives the model the
clues that make that possible: the real on-screen text (button labels,
headings, messages -- kg/ts_parser.py extracts it from JSX), file names (a
Next.js `app/product/[handle]` folder is "the page for one product"), and an
outline of the whole app so each module is named in context.

Modules that only work behind the scenes (data access, configuration, types,
icons, helpers) are marked `kind = "supporting"`, so the UI can tuck them away
rather than list them beside real features -- there is nothing for a BA to
test in them. Each module also gets a one-sentence description.

The technical module name (the `domain` grouping kg/dev_graph_builder.py
computes -- a dotted package path) is never replaced. It's the stable key
everything else keys off of (module_test_context's `module` query param,
stored test cases' `module` column, Compare Runs). Names are a purely cosmetic
layer stored in server/db.py's module_labels, keyed by (repo_id, module) and
not tied to a run, so they survive re-analysis untouched.
"""
from __future__ import annotations

import json
import re

from server.llm_gap_analysis import is_configured  # same OPENAI_API_KEY gates all the LLM features

DEFAULT_MODEL = "gpt-4o-mini"
ALLOWED_KINDS = {"feature", "supporting"}

# Per-module clue caps -- enough signal to tell what a module is for, small enough
# that a 200-module repo's prompt stays reasonable.
_MAX_SAMPLE_NAMES = 8
_MAX_UI_TEXT = 10
_MAX_FILES = 8
# Modules named per call. One call for every module of a very large repo would run
# past the model's output limit; the full outline still goes into every batch, so a
# module is always named with the whole app in view.
_BATCH_SIZE = 60
_MAX_TOKENS_PER_BATCH = 8000
_RENAME_ROUNDS = 2  # re-asks for names that still sound like code


def _leaf(file_label: str, module: str) -> str:
    """`components.cart.add-to-cart` in module `components.cart` -> `add-to-cart`."""
    prefix = module + "."
    return file_label[len(prefix):] if file_label.startswith(prefix) else file_label.rsplit(".", 1)[-1]


def _summarize_modules(modules: list[dict]) -> list[dict]:
    """Trims gap_analysis_context()'s full per-file/class/method structure to one
    compact clue-sheet per module. Everything is real (never invented), just not
    exhaustively listed; empty clues are omitted to keep the prompt small."""
    summaries = []
    for m in modules:
        names: list[str] = []
        ui_text: list[str] = []
        purpose = None
        for f in m["files"]:
            if purpose is None and f.get("purpose"):
                purpose = f["purpose"]
            names.extend(f["functions"])
            names.extend(c["name"] for c in f["classes"])
            for t in f.get("ui_text", []):
                if t not in ui_text:
                    ui_text.append(t)
        summary = {
            "module": m["module"],
            "files": [_leaf(f["file"], m["module"]) for f in m["files"]][:_MAX_FILES],
            "sample_names": names[:_MAX_SAMPLE_NAMES],
            "ui_text": ui_text[:_MAX_UI_TEXT],
            "purpose": purpose,
        }
        summaries.append({k: v for k, v in summary.items() if v})
    return summaries


def _build_prompt(batch: list[dict], outline: list[str]) -> str:
    return f"""You are naming the parts of a software product for a NON-TECHNICAL business analyst or product owner. They know the product from USING it -- the pages, screens and features a person sees (for an online shop: Home Page, Search, Product Page, Shopping Cart, Checkout, Navigation Bar) -- and they cannot read code, so folder names like "components.layout.navbar" or "lib.shopify.queries" mean nothing to them.

APP OUTLINE -- every module in this codebase, by technical name, for orientation only (you are naming only the ones listed further down):
{json.dumps(outline)}

MODULES TO NAME -- each is a group of source files, with clues: "files" (file names; a folder like app/product/[handle] usually means "the page for one product"), "sample_names" (real function/class names), "ui_text" (the actual words shown on screen -- your strongest evidence), and a "purpose" if known:
---
{json.dumps(batch, indent=2)}
---

HOW TO READ THE CLUES
- Web-app folders map to what a visitor sees. A file called "page" (or "index") at the top of the app/route folder is the site's HOME PAGE. A "page" inside a folder is the page at that address (app/search = the Search page). A folder in [brackets] is a page for ONE item (app/product/[handle] = the page for a single product; [page] = an information page such as About or Terms). "layout" is the shared frame around pages (header, footer). Folders under "api" are behind-the-scenes endpoints.
- Anything that DRAWS part of a screen -- a footer, a banner, a product grid, a carousel, a header -- is a feature, even a small one: name it "Page Footer", "Product Carousel". "supporting" is only for code that draws nothing: fetching data, connections to other systems, settings, types, helpers.
- A module can mix things: if its files include the site's home "page", it is the Home Page (mention the shared frame in the description) -- not "Application".

For EACH module return:
- "name": what a person would call this part of the product -- the SCREEN or FEATURE it powers -- in plain everyday words, Title Case, 1 to 4 words: "Home Page", "Shopping Cart", "Product Page", "Navigation Bar", "Search & Filters". Take the words from what's on screen (ui_text) and from route/file names. For a "supporting" module, name the JOB in business words -- "Store Connection", "Product Data Retrieval", "Site Settings", "Icons & Graphics" -- not the technology. NEVER use developer words or technology names: module, component(s), util, helper, lib, library, handler, service, fragment, query, mutation, config, configuration, API, wrapper, application, Next.js, React, PostCSS. Bad names, for illustration: "Components", "Library", "Application", "Shopify Queries", "Next.js Configuration".
- "kind": "feature" if a person could see or use it directly; "supporting" if it only works behind the scenes -- fetching data, connections to other systems, configuration, shared types, icons and styling, helper code, test code.
- "description": ONE plain-English sentence for the analyst. For a feature, what the person can do or see here ("Add, remove and change the quantity of items, then continue to checkout"). For a supporting module, what it does for the product ("Fetches product and cart information from the Shopify store").
Where two modules are parts of the same screen, keep the screen's name and add the part: "Product Page - Photo Gallery", "Product Page - Options". Otherwise don't force artificial distinctiveness. Never leave a module out. Don't claim features the clues don't support.

Respond with ONLY a JSON object of this exact shape, one entry per module listed above, keyed by the module's technical name exactly as given:
{{"modules": {{"<module>": {{"name": "...", "kind": "feature" | "supporting", "description": "..."}}, ...}}}}"""


def _clean_entry(value) -> dict | None:
    """One model entry -> {name, kind, description}, or None if unusable. A missing
    or odd `kind` defaults to feature -- better to show a module than to hide one."""
    if isinstance(value, str):  # tolerate a bare name
        value = {"name": value}
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
        return None
    kind = value.get("kind")
    description = value.get("description")
    return {
        "name": value["name"].strip(),
        "kind": kind if kind in ALLOWED_KINDS else "feature",
        "description": description.strip() if isinstance(description, str) and description.strip() else None,
    }


# Words that mark a name as a developer's, not a business analyst's. A prompt rule
# against them was ignored in practice (names like "Components", "Shopify Queries",
# "Next.js Configuration" came back), so it's CHECKED after the fact and only the
# offending names are re-asked, once.
_DEV_WORDS = re.compile(
    r"\b(modules?|components?|utils?|utilit(y|ies)|helpers?|lib|librar(y|ies)|handlers?|services?|fragments?|"
    r"quer(y|ies)|mutations?|config|configuration|api|wrappers?|applications?|next\.?js|react|postcss|"
    r"typescript|javascript|webpack|tailwind)\b",
    re.I,
)


def _dev_speak(name: str) -> bool:
    return bool(_DEV_WORDS.search(name))


# The folder that holds the site's root route (Next.js `app/`, `src/app/`, `pages/`): its
# `page` (or `index`) file IS the home page. gpt-4o-mini kept calling it "Site Layout" or
# "Application" even with that rule in the prompt, and it's the one screen every BA looks
# for first -- so this is decided from the file names, not left to the model.
_ROUTE_ROOTS = {"app", "pages"}


def _is_home_page_module(summary: dict) -> bool:
    return summary["module"].rsplit(".", 1)[-1] in _ROUTE_ROOTS and any(f in ("page", "index") for f in summary.get("files", []))


def _build_rename_prompt(batch: list[dict], outline: list[str], current: dict[str, str]) -> str:
    listed = "\n".join(f'  - "{m}" is currently called "{n}"' for m, n in current.items())
    return _build_prompt(batch, outline) + f"""

FOLLOW-UP PASS -- these modules were already named, but the names use developer words a business analyst wouldn't say:
{listed}
Rename ONLY these, following every rule above. Describe the job in the words a person who has only ever used the product would use ("Store Connection", not "Shopify Queries"; "Page Footer", not "Layout Elements"; "Site Settings", not "Next.js Configuration"). Respond in the same JSON shape with only these modules."""


def _ask(client, model: str, prompt: str, known: set[str]) -> dict[str, dict]:
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=_MAX_TOKENS_PER_BATCH,
        )
    except Exception as e:  # noqa: BLE001 -- surface any API failure (auth, rate limit, network) plainly
        raise RuntimeError(f"OpenAI API call failed: {e}") from e

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model didn't return valid JSON: {e}") from e
    entries = parsed.get("modules", {})
    if not isinstance(entries, dict):
        raise RuntimeError("Model's response had an unexpected shape (modules wasn't an object).")
    out = {}
    for module, value in entries.items():
        cleaned = _clean_entry(value) if module in known else None
        if cleaned:
            out[module] = cleaned
    return out


def generate_module_names(context: dict, model: str | None = None) -> dict[str, dict]:
    """Names every module in `context["modules"]` (from
    kg.doc_gaps.gap_analysis_context, ideally with include_ui_text=True) and
    returns {module: {name, kind, description}}, restricted to modules actually
    present in the input. Batches large repos. Raises RuntimeError with a message
    safe to show the user on any failure -- missing key, API error, or a
    malformed response."""
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY isn't configured on this deployment.")

    modules = context.get("modules") or []
    if not modules:
        return {}
    known = {m["module"] for m in modules}
    summaries = _summarize_modules(modules)
    outline = [s["module"] for s in summaries]
    model = model or DEFAULT_MODEL

    from openai import OpenAI  # lazy import: only needed when this is actually called

    client = OpenAI()
    out: dict[str, dict] = {}
    for i in range(0, len(summaries), _BATCH_SIZE):
        batch = summaries[i:i + _BATCH_SIZE]
        out.update(_ask(client, model, _build_prompt(batch, outline), known))

    # Verify, don't trust: re-ask once, for just the names that still sound like code.
    by_module = {s["module"]: s for s in summaries}
    for _ in range(_RENAME_ROUNDS):
        offenders = [m for m, e in out.items() if _dev_speak(e["name"])]
        if not offenders:
            break
        for i in range(0, len(offenders), _BATCH_SIZE):
            chunk = offenders[i:i + _BATCH_SIZE]
            prompt = _build_rename_prompt([by_module[m] for m in chunk], outline, {m: out[m]["name"] for m in chunk})
            for module, entry in _ask(client, model, prompt, set(chunk)).items():
                out[module] = entry  # a retry that's still imperfect beats the first try

    for module, entry in out.items():
        if _is_home_page_module(by_module[module]) and "home" not in entry["name"].lower():
            entry.update(name="Home Page", kind="feature")
    return out
