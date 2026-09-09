"""Mechanical, LLM-free parser for arbitrary Python codebases -- not tied to
any framework, works on any repo that's valid Python. Uses Python's own
`ast` module (stdlib, always correct, zero tokens spent) to extract:

  Module   -- one per .py file
  Class    -- class definitions (top-level)
  Function -- top-level functions
  Method   -- functions defined inside a class

...and the structural edges between them: IMPORTS (module-to-module, internal
imports only), DEFINES (module/class owns its functions/classes), CALLS
(best-effort -- direct name calls, self.method() calls, and
imported-alias.function() calls, resolved against a symbol table built from
this same repo).

This is the "AST does structure" half of the hybrid pipeline described in
TestAtlas's dev-code work: purpose summaries and non-obvious relationships
are a separate, LLM-driven enrichment pass layered on top of this mechanical
skeleton -- not attempted here, and this module is fully useful without it.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

_IGNORED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", "site-packages",
}


@dataclass
class PyFunction:
    name: str
    qualname: str  # "module.dotted.name:func" or "module.dotted.name:Class.method"
    is_method: bool
    class_qualname: str | None
    calls: list[str] = field(default_factory=list)  # resolved qualnames of things it calls
    lineno: int = 0


@dataclass
class PyClass:
    name: str
    qualname: str
    bases: list[str] = field(default_factory=list)  # best-effort dotted/plain names
    methods: list[str] = field(default_factory=list)  # qualnames, filled in after extraction
    lineno: int = 0


@dataclass
class PyModule:
    path: str  # relative path from repo root
    dotted_name: str
    imports: list[str] = field(default_factory=list)  # dotted names of OTHER modules in this repo
    classes: list[PyClass] = field(default_factory=list)
    functions: list[PyFunction] = field(default_factory=list)  # top-level only


@dataclass
class ParsedPythonRepo:
    modules: dict[str, PyModule] = field(default_factory=dict)  # keyed by dotted_name
    skipped_files: list[str] = field(default_factory=list)  # syntax errors etc.


def _is_ignored(path: Path) -> bool:
    return any(part in _IGNORED_DIR_NAMES for part in path.parts)


def _dotted_name_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def _resolve_relative_import(current_dotted: str, level: int, module: str | None) -> str | None:
    """Best-effort resolution of `from .foo import bar` / `from ..pkg.foo import bar`."""
    current_parts = current_dotted.split(".")
    # level=1 means "same package as current module" -- current module's own
    # package is everything but its last segment (unless it's a package __init__,
    # which we can't distinguish here without the original filename; close enough).
    base = current_parts[: max(0, len(current_parts) - level)]
    if module:
        base += module.split(".")
    return ".".join(base) if base else None


def _longest_known_prefix(dotted: str, known: set[str]) -> str | None:
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known:
            return candidate
    return None


def parse_repo(root: Path) -> ParsedPythonRepo:
    result = ParsedPythonRepo()
    py_files = sorted(p for p in root.rglob("*.py") if not _is_ignored(p.relative_to(root)))

    parsed_trees: dict[str, tuple[Path, ast.Module]] = {}
    for path in py_files:
        dotted = _dotted_name_for(path, root)
        try:
            tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
        except SyntaxError:
            result.skipped_files.append(str(path.relative_to(root)))
            continue
        parsed_trees[dotted] = (path, tree)
        result.modules[dotted] = PyModule(path=str(path.relative_to(root)), dotted_name=dotted)

    known_modules = set(result.modules)
    # name -> "module_dotted:original_name" (from-imports) or "module_dotted" (whole-module imports)
    bindings_by_module: dict[str, dict[str, str]] = {}

    # Pass 1: imports + skeleton (classes/functions, no call resolution yet --
    # that needs every module's symbol table to exist first).
    for dotted, (path, tree) in parsed_trees.items():
        py_module = result.modules[dotted]
        bindings: dict[str, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _longest_known_prefix(alias.name, known_modules)
                    if target:
                        py_module.imports.append(target)
                        bindings[alias.asname or alias.name.split(".")[0]] = target
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    target = _resolve_relative_import(dotted, node.level, node.module)
                else:
                    target = _longest_known_prefix(node.module or "", known_modules)
                if target and target in known_modules:
                    py_module.imports.append(target)
                    for alias in node.names:
                        local = alias.asname or alias.name
                        bindings[local] = f"{target}:{alias.name}"

        py_module.imports = sorted(set(py_module.imports) - {dotted})
        bindings_by_module[dotted] = bindings

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                cls_qual = f"{dotted}:{node.name}"
                bases = [_unparse_safe(b) for b in node.bases]
                py_class = PyClass(name=node.name, qualname=cls_qual, bases=bases, lineno=node.lineno)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_qual = f"{dotted}:{node.name}.{item.name}"
                        py_class.methods.append(method_qual)
                        py_module.functions.append(
                            PyFunction(
                                name=item.name, qualname=method_qual, is_method=True,
                                class_qualname=cls_qual, lineno=item.lineno,
                            )
                        )
                py_module.classes.append(py_class)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_qual = f"{dotted}:{node.name}"
                py_module.functions.append(
                    PyFunction(name=node.name, qualname=func_qual, is_method=False,
                               class_qualname=None, lineno=node.lineno)
                )

    # Build a global symbol table: qualname suffix -> full qualname, per module,
    # so call resolution can check "does this name exist in module X".
    module_symbols: dict[str, dict[str, str]] = {}
    for dotted, py_module in result.modules.items():
        symbols: dict[str, str] = {}
        for fn in py_module.functions:
            if not fn.is_method:
                symbols[fn.name] = fn.qualname
        for cls in py_module.classes:
            symbols[cls.name] = cls.qualname
        module_symbols[dotted] = symbols

    # Pass 2: resolve calls now that the full symbol table exists.
    for dotted, (path, tree) in parsed_trees.items():
        py_module = result.modules[dotted]
        bindings = bindings_by_module[dotted]
        own_symbols = module_symbols[dotted]
        methods_by_class: dict[str, set[str]] = {}
        for cls in py_module.classes:
            methods_by_class[cls.qualname] = {m.rsplit(".", 1)[-1] for m in cls.methods}

        fn_by_qualname = {fn.qualname: fn for fn in py_module.functions}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            enclosing_qual = _enclosing_qualname(tree, node, dotted)
            fn = fn_by_qualname.get(enclosing_qual)
            if fn is None:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                resolved = _resolve_call(call, bindings, own_symbols, module_symbols, fn.class_qualname, methods_by_class)
                if resolved:
                    fn.calls.append(resolved)
            fn.calls = sorted(set(fn.calls))

    return result


def _unparse_safe(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "<expr>"


def _enclosing_qualname(tree: ast.Module, target: ast.AST, dotted: str) -> str | None:
    """Finds the qualname we assigned to `target` during skeleton extraction,
    by walking parent classes the same way pass 1 did."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is target:
            return f"{dotted}:{node.name}"
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item is target:
                    return f"{dotted}:{node.name}.{item.name}"
    return None


def _resolve_call(
    call: ast.Call,
    bindings: dict[str, str],
    own_symbols: dict[str, str],
    module_symbols: dict[str, dict[str, str]],
    class_qualname: str | None,
    methods_by_class: dict[str, set[str]],
) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        if func.id in own_symbols:
            return own_symbols[func.id]
        if func.id in bindings:
            target = bindings[func.id]
            if ":" in target:
                mod, name = target.split(":", 1)
                return module_symbols.get(mod, {}).get(name)
            return None  # whole-module import used as a bare name -- not a direct call we can resolve
        return None  # external/stdlib call -- not modeled

    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            if func.value.id == "self" and class_qualname and func.attr in methods_by_class.get(class_qualname, set()):
                return f"{class_qualname}.{func.attr}"
            if func.value.id in bindings:
                target = bindings[func.value.id]
                mod = target.split(":", 1)[0] if ":" in target else target
                return module_symbols.get(mod, {}).get(func.attr)
    return None
