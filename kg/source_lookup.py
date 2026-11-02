"""Source symbol lookup plus bounded full-file context retrieval.
Python and TypeScript/JavaScript function bodies support exact-quote validation.
Module/journey gathering expands related files and reports omissions explicitly.
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
        if data.get("type") == "Function" and _qualname_from_node_id(_node_id) == qualified_name:
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
        return _extract_ts_source(file_path, qualified_name)
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


def _extract_ts_source(file_path: Path, qualified_name: str) -> str | None:
    from .ts_parser import TS_SUFFIXES, _TS_GRAMMAR_SUFFIXES, _get_parsers, _function_like
    if file_path.suffix.lower() not in TS_SUFFIXES:
        return None
    try:
        from tree_sitter import Parser
        parser = Parser(_get_parsers()['ts' if file_path.suffix in _TS_GRAMMAR_SUFFIXES else 'tsx'].language)
        raw = file_path.read_bytes()
        tree = parser.parse(raw)
        if tree.root_node.has_error:
            return None
        matches = []
        def visit(node, owner=''):
            if node.type in ('class_declaration','abstract_class_declaration','class'):
                name = node.child_by_field_name('name')
                owner = name.text.decode() if name else 'default'
            name = node.child_by_field_name('name')
            label = name.text.decode() if name else ''
            if node.type in ('function_declaration','generator_function_declaration'):
                if label == qualified_name: matches.append(node)
            elif node.type == 'variable_declarator' and _function_like(node.child_by_field_name('value')) is not None:
                if label == qualified_name: matches.append(node)
            elif node.type in ('method_definition','public_field_definition','field_definition'):
                if owner + '.' + label == qualified_name: matches.append(node)
            for child in node.named_children:
                visit(child, owner)
        visit(tree.root_node)
        if len(matches) == 1:
            return raw[matches[0].start_byte:matches[0].end_byte].decode('utf-8')
    except (OSError, UnicodeError, RuntimeError, ImportError):
        return None
    return None


def find_citation_domain_and_path(g, qualified_name: str) -> tuple[str, str] | None:
    """Like find_citation_path, but for a citation that isn't pre-scoped to
    one module -- a Story Coverage requirement's `covers`, unlike a
    verification pass's, come from a context spanning the WHOLE graph
    (kg.doc_gaps.gap_analysis_context never scopes to one module), so there's
    no module to filter by yet. Searches every domain, returns (domain, path)
    for the first match."""
    matches = [(data.get("domain"), data.get("path")) for node_id,data in g.nodes(data=True)
               if (data.get("type") == "Function" and _qualname_from_node_id(node_id) == qualified_name)
               or (data.get("type") == "Class" and "." not in qualified_name and data.get("label") == qualified_name)]
    return matches[0] if len(matches) == 1 else None


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
        if data.get("type") == "Function" and _qualname_from_node_id(node_id) == qualified_name:
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
    from .source_evidence import gather_source_evidence
    return gather_source_evidence(g, source_root, node_ids=journey.get("node_ids") or [], max_files=min(max_functions, 24))


def gather_module_sources(g, source_root: Path, modules: list[str], max_functions: int = 40, query: str = "") -> dict[str, str]:
    """Real source for every Function node whose `domain` is one of `modules`
    -- used by the retrieval-narrowed Story Coverage judgment pass
    (server/llm_story_coverage.py), which only shows the model source for the
    handful of modules retrieval already picked as relevant, not the whole
    codebase. Capped, same reasoning as gather_journey_sources."""
    from .source_evidence import gather_source_evidence
    return gather_source_evidence(g, source_root, modules=modules, query=query, max_files=min(max_functions, 24))


def resolve_cited_paths(g, names: list[str], module: str | None = None) -> list[str | None]:
    """Resolves each name in `names` to its real repo-relative file path (None
    if it can't be found) -- purely for evidence provenance (server/db.py's
    cited_paths column on test_cases/story_requirements), never changes what
    was cited, just records where each one lives. `module`-scoped via
    find_citation_path when given (a test case's module is already known by
    the time it's verified); otherwise searched across every domain via
    find_citation_domain_and_path (a story requirement has no single module)."""
    out: list[str | None] = []
    for name in names:
        if module:
            out.append(find_citation_path(g, module, name))
        else:
            found = find_citation_domain_and_path(g, name)
            out.append(found[1] if found else None)
    return out


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
