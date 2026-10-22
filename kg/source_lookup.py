"""Reads a specific function or method's real source code off disk, for the
test-case verification pass (server/llm_test_verification.py). The knowledge
graph itself never stores function bodies or line numbers -- kept lightweight
for clustering/naming/generation, none of which need to read behavior, only
structure -- so this re-parses the one file a citation points to and extracts
just that function, not the whole file or repo. Python only for now: a TS/JS
citation falls back to None (judged on names alone), not an error -- that
would need kg/ts_parser.py's tree-sitter parse instead of the `ast` module.
"""
from __future__ import annotations

import ast
from pathlib import Path


def find_citation_path(g, module: str, qualified_name: str) -> str | None:
    """Given a canonical citation ("Cart.add" or "checkout", already resolved
    by kg.doc_gaps.canonical_cover) and the module (domain) it was cited
    against, finds the real repo-relative file path from the graph -- every
    node in one file shares the identical `path` attribute, so matching
    either the method's own Function node (for "Class.method") or a top-level
    Function/Class node (for a bare name) is enough to locate the file."""
    owner, dot, method = qualified_name.partition(".")
    target_label = method if dot else owner
    for _node_id, data in g.nodes(data=True):
        if data.get("domain") != module:
            continue
        if data.get("type") == "Function" and data.get("label") == target_label:
            return data.get("path")
        if not dot and data.get("type") == "Class" and data.get("label") == target_label:
            return data.get("path")
    return None


def extract_function_source(file_path: Path, qualified_name: str) -> str | None:
    """`qualified_name` is already-canonicalized: a bare top-level function
    name ("checkout") or "Class.method". Returns None if the file isn't
    Python, can't be read, can't be parsed, or the name isn't found inside it
    -- never raises, since a verification pass should degrade to judging on
    names alone rather than fail outright."""
    if file_path.suffix != ".py":
        return None
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return None

    owner, dot, method = qualified_name.partition(".")

    if dot:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == owner:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == method:
                        return ast.get_source_segment(source, sub)
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == owner:
            return ast.get_source_segment(source, node)
    return None


def find_citation_domain_and_path(g, qualified_name: str) -> tuple[str, str] | None:
    """Like find_citation_path, but for a citation that isn't pre-scoped to
    one module -- a Story Coverage requirement's `covers`, unlike a
    verification pass's, come from a context spanning the WHOLE graph
    (kg.doc_gaps.gap_analysis_context never scopes to one module), so there's
    no module to filter by yet. Searches every domain, returns (domain, path)
    for the first match."""
    owner, dot, method = qualified_name.partition(".")
    target_label = method if dot else owner
    for _node_id, data in g.nodes(data=True):
        if data.get("type") == "Function" and data.get("label") == target_label:
            return data.get("domain"), data.get("path")
        if not dot and data.get("type") == "Class" and data.get("label") == target_label:
            return data.get("domain"), data.get("path")
    return None


def gather_requirement_sources(g, source_root: Path, requirement: dict) -> dict[str, str]:
    """Same idea as gather_cited_sources, for one Story Coverage requirement
    instead of a list of module-scoped test cases -- uses the domain-agnostic
    finder since the requirement isn't tied to a single module either."""
    sources: dict[str, str] = {}
    for name in requirement.get("covers") or []:
        found = find_citation_domain_and_path(g, name)
        if not found:
            continue
        _domain, rel_path = found
        src = extract_function_source(source_root / rel_path, name)
        if src:
            sources[name] = src
    return sources


def resolve_citation_node_id(g, qualified_name: str) -> str | None:
    """Like find_citation_domain_and_path, but returns the graph node id
    itself rather than (domain, path) -- for checking whether a citation
    falls inside a journey's `node_ids` set (kg/journeys.py), which is keyed
    by node id, not by name."""
    owner, dot, method = qualified_name.partition(".")
    target_label = method if dot else owner
    for node_id, data in g.nodes(data=True):
        if data.get("type") == "Function" and data.get("label") == target_label:
            return node_id
        if not dot and data.get("type") == "Class" and data.get("label") == target_label:
            return node_id
    return None


def _qualname_from_node_id(node_id: str) -> str:
    """"function:app.checkout:CheckoutService.pay" -> "CheckoutService.pay" --
    node ids are "function:<dotted module>:<name or Class.method>"."""
    return node_id.split(":", 2)[-1]


def gather_journey_sources(g, source_root: Path, journey: dict, max_functions: int = 40) -> dict[str, str]:
    """Real source for every Function node inside a journey's reachable set
    (kg.journeys.detect_journeys's `node_ids`) -- not just already-cited
    names, since the derivation pass (server/llm_journey_requirements.py)
    needs to discover NEW patterns in code nobody has cited yet. Capped: a
    large journey can reach hundreds of functions, and the prompt needs to
    stay a reasonable size."""
    sources: dict[str, str] = {}
    for node_id in journey.get("node_ids") or []:
        if len(sources) >= max_functions:
            break
        data = g.nodes.get(node_id)
        if not data or data.get("type") != "Function" or not data.get("path"):
            continue
        qualified_name = _qualname_from_node_id(node_id)
        src = extract_function_source(source_root / data["path"], qualified_name)
        if src:
            sources[qualified_name] = src
    return sources


def gather_module_sources(g, source_root: Path, modules: list[str], max_functions: int = 40) -> dict[str, str]:
    """Real source for every Function node whose `domain` is one of `modules`
    -- used by the retrieval-narrowed Story Coverage judgment pass
    (server/llm_story_coverage.py), which only shows the model source for the
    handful of modules retrieval already picked as relevant, not the whole
    codebase. Capped, same reasoning as gather_journey_sources."""
    wanted = set(modules)
    sources: dict[str, str] = {}
    for node_id, data in g.nodes(data=True):
        if len(sources) >= max_functions:
            break
        if data.get("type") != "Function" or data.get("domain") not in wanted or not data.get("path"):
            continue
        qualified_name = _qualname_from_node_id(node_id)
        src = extract_function_source(source_root / data["path"], qualified_name)
        if src:
            sources[qualified_name] = src
    return sources


def gather_cited_sources(g, module: str, source_root: Path, cases: list[dict]) -> dict[str, str]:
    """One lookup per distinct cited name across all of `cases` (each test
    case's already-validated `covers` list) -- not per case, since several
    cases often cite the same function. Entries that can't be resolved (a
    cross-module citation from `parts`, a non-Python file, a name that moved)
    are simply absent, not an error."""
    names = {n for c in cases for n in (c.get("covers") or [])}
    sources: dict[str, str] = {}
    for name in names:
        rel_path = find_citation_path(g, module, name)
        if not rel_path:
            continue
        src = extract_function_source(source_root / rel_path, name)
        if src:
            sources[name] = src
    return sources
