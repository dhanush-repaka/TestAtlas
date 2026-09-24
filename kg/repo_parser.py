"""Parses a whole checkout -- every supported language in it -- into one
ParsedRepo (kg/code_model.py) for the graph builder.

Python goes through the stdlib `ast` module (kg/python_ast_parser.py), and
TypeScript/JavaScript through tree-sitter (kg/ts_parser.py). Both fill in the
same language-neutral shapes, so a repo with, say, a Python backend and a
TypeScript frontend becomes ONE graph: one set of module scores, one set of
findings, one set of LLM features, with each file tagged by its `language`.

Supported source files, and what counts as one, are defined here once so the
runner, the folder-upload endpoint and the UI's upload filter all agree.
"""
from __future__ import annotations

from pathlib import Path

from . import python_ast_parser, ts_parser
from .code_model import ParsedRepo

PYTHON_SUFFIX = ".py"
SOURCE_SUFFIXES = (PYTHON_SUFFIX, *ts_parser.TS_SUFFIXES)


def is_source_file(name: str) -> bool:
    return name.endswith(PYTHON_SUFFIX) or ts_parser.is_ts_source(name)


def is_ignored_path(rel: Path) -> bool:
    """Whether a repo-relative path sits under a directory the parsers skip.
    Python's ignore list is unchanged from before TypeScript support existed;
    TS/JS additionally skips framework build output and vendored JavaScript
    (see kg/ts_parser.py) -- so a `vendor/` package of real Python is still
    parsed, exactly as before."""
    if rel.name.endswith(PYTHON_SUFFIX):
        return any(part in python_ast_parser._IGNORED_DIR_NAMES for part in rel.parts)
    return ts_parser.is_ignored_path(rel)


def has_source_files(root: Path) -> bool:
    from .code_model import iter_files
    ignored = ts_parser.IGNORED_DIRS  # superset -- good enough for "is there anything to analyze at all"
    return next(iter_files(root, ignored, is_source_file), None) is not None


def parse_repo(root: Path) -> ParsedRepo:
    result = python_ast_parser.parse_repo(root)
    ts = ts_parser.parse_ts_repo(root, taken=set(result.modules))
    result.modules.update(ts.modules)
    result.skipped_files += ts.skipped_files
    return result
