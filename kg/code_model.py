"""Language-neutral shapes every source parser produces and the graph builder
consumes (kg/dev_graph_builder.py), plus the one file-discovery helper they
share.

Originally these lived in kg/python_ast_parser.py under Python-specific names;
they never actually depended on Python -- a module has a path and a dotted
name, imports other modules in the same repo, and defines classes and
functions (methods are functions owned by a class). Pulling them out lets a
second parser (kg/ts_parser.py, TypeScript/JavaScript via tree-sitter) fill in
the same structures, so scoring, findings, the LLM features and the UI all
work on any supported language without knowing which one produced a file.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CodeFunction:
    name: str
    qualname: str  # "module.dotted.name:func" or "module.dotted.name:Class.method"
    is_method: bool
    class_qualname: str | None
    calls: list[str] = field(default_factory=list)  # resolved qualnames of things it calls
    lineno: int = 0


@dataclass
class CodeClass:
    name: str
    qualname: str
    bases: list[str] = field(default_factory=list)  # best-effort dotted/plain names
    methods: list[str] = field(default_factory=list)  # qualnames, filled in after extraction
    lineno: int = 0


@dataclass
class CodeModule:
    path: str  # relative path from repo root
    dotted_name: str
    imports: list[str] = field(default_factory=list)  # dotted names of OTHER modules in this repo
    classes: list[CodeClass] = field(default_factory=list)
    functions: list[CodeFunction] = field(default_factory=list)  # top-level functions AND methods
    language: str = "python"  # "python" | "typescript" | "javascript"
    ui_text: list[str] = field(default_factory=list)  # visible strings in JSX (labels, headings, placeholders) -- see kg/ts_parser.py


@dataclass
class ParsedRepo:
    modules: dict[str, CodeModule] = field(default_factory=dict)  # keyed by dotted_name
    skipped_files: list[str] = field(default_factory=list)  # syntax errors, unreadable, oversized, ...


def iter_files(root: Path, ignored_dirs: set[str] | frozenset[str], wanted: Callable[[str], bool]) -> Iterator[Path]:
    """Every file under `root` whose *name* passes `wanted`, never descending
    into a directory named in `ignored_dirs`. Pruning while walking (rather
    than `rglob` then filtering) matters for a local JavaScript/TypeScript
    checkout: `node_modules` routinely holds 100k+ files, and a walk that
    enters it just to throw its contents away is many seconds of wasted I/O."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for name in filenames:
            if wanted(name):
                yield Path(dirpath) / name
