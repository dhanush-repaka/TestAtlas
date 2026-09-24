"""Folder-upload source: analyze a folder that lives on the *browser's*
machine, for a deployed instance (Fly, a VPS) where a "Local folder" path
would point at the server's own disk, not your files.

The browser sends the folder's source files (Python, TypeScript, JavaScript) in batches; they're
written under the repo's own workspace directory (the same place a Git clone
would go -- see runner.resolve_source_dir) and analyzed exactly like any other
checkout. It's a snapshot, not a live link: re-upload to pick up later changes.

This writes attacker-shaped input (filenames) to disk, so it's deliberately
strict rather than permissive:
  * only source files the parsers actually read are ever stored (`.py`, and
    TypeScript/JavaScript minus `.d.ts` declarations and `.min.` bundles) --
    that keeps an upload small and inert (no scripts, binaries, or dotfiles);
  * every relative path is normalized and rejected if it's absolute, has a
    drive letter, contains `..`, or would resolve outside the repo's folder;
  * the parsers' own ignore rules (.git, node_modules, venvs, framework build
    output, vendored JS, ...) are applied here too, so a client that doesn't
    filter can't bloat the volume;
  * per-file, per-repo file-count, and per-repo total-size caps, sized for a
    512MB machine with a 2GB volume rather than for the biggest monorepo.
"""
from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path, PurePosixPath

from kg.repo_parser import is_ignored_path, is_source_file

from .runner import WORKSPACE_DIR

MAX_FILE_BYTES = 2 * 1024 * 1024        # a single source file bigger than this is not real source
MAX_FILES_PER_REPO = 20_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024


class UploadTooLarge(ValueError):
    """Raised (message safe to show the user) when a repo's upload passes the file-count or size cap."""


def upload_dir(repo_id: str) -> Path:
    return WORKSPACE_DIR / repo_id


def remove(repo_id: str) -> None:
    shutil.rmtree(upload_dir(repo_id), ignore_errors=True)


def clean_relative_path(raw: str) -> PurePosixPath | None:
    """Normalizes a client-supplied relative path to a safe POSIX path, or None
    if it could escape the repo's folder (absolute, drive-lettered, or `..`)."""
    if not raw or "\x00" in raw:
        return None
    p = raw.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if not parts or any(s == ".." for s in parts):
        return None
    return PurePosixPath(*parts)


def dir_stats(base: Path) -> tuple[int, int]:
    """(file count, total bytes) of what's already uploaded for this repo."""
    count = size = 0
    if base.is_dir():
        for p in base.rglob("*"):
            if p.is_file():
                count += 1
                size += p.stat().st_size
    return count, size


def save_files(repo_id: str, items: list[tuple[str, bytes]], reset: bool) -> dict:
    """Writes one batch of (relative path, content) pairs. `reset` (sent with
    a re-upload's first batch) wipes the previous snapshot first. Returns
    {saved, skipped: {reason: n}, total_files, total_bytes}; raises
    UploadTooLarge if the repo would exceed its caps."""
    base = upload_dir(repo_id)
    if reset:
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    base_resolved = base.resolve()

    count, size = dir_stats(base)
    skipped: Counter[str] = Counter()
    saved = 0

    for name, data in items:
        rel = clean_relative_path(name)
        if rel is None:
            skipped["invalid_path"] += 1
            continue
        if not is_source_file(rel.name):
            skipped["not_source"] += 1
            continue
        if is_ignored_path(Path(*rel.parts)):
            skipped["ignored_dir"] += 1
            continue
        if len(data) > MAX_FILE_BYTES:
            skipped["too_large"] += 1
            continue

        dest = base / Path(*rel.parts)
        previous = dest.stat().st_size if dest.is_file() else None
        new_count = count + (0 if previous is not None else 1)
        new_size = size - (previous or 0) + len(data)
        if new_count > MAX_FILES_PER_REPO:
            raise UploadTooLarge(f"This folder has more than {MAX_FILES_PER_REPO:,} source files -- too many to upload.")
        if new_size > MAX_TOTAL_BYTES:
            raise UploadTooLarge(f"This folder's source files total more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB -- too large to upload.")

        try:
            if not dest.resolve().is_relative_to(base_resolved):  # defense in depth on top of clean_relative_path
                skipped["invalid_path"] += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError:  # e.g. "a.py" already a file where "a.py/b.py" needs a directory
            skipped["invalid_path"] += 1
            continue
        count, size = new_count, new_size
        saved += 1

    return {"saved": saved, "skipped": dict(skipped), "total_files": count, "total_bytes": size}
