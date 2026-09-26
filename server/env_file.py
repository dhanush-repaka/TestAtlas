"""Optional `.env` file for settings and secrets -- for running TestAtlas on a
machine you control (a laptop, a company server) instead of a platform that has a
secrets store.

The rules are deliberately narrow, because the same code also runs the hosted site:

  * Real environment variables ALWAYS win. A key already set in the process
    environment (Fly's `fly secrets`, `docker run -e`, your shell) is never
    overwritten by the file. So a deployment that sets its values the normal way is
    unaffected even if a stray `.env` exists.
  * No `.env` file, no effect at all.
  * Values are never printed or logged -- only the NAMES of what was loaded.
  * The file is looked up at `TESTATLAS_ENV_FILE` if set (empty string disables
    loading), else `.env` in the project root.

Loaded from server/__init__.py, i.e. before anything else in `server` reads the
environment (db.py reads DATA_ROOT at import time).
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(text: str) -> dict[str, str]:
    """KEY=value lines. Supports `export KEY=...`, single/double quotes (which keep
    `#` and surrounding spaces), and `# comments` -- full-line, or after an unquoted
    value. Bad lines are skipped rather than aborting startup."""
    out: dict[str, str] = {}
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def load_env_file(path: Path | str | None = None, environ=None) -> list[str]:
    """Applies the file to `environ` (default os.environ) without overriding anything
    already set; returns the names it actually set. Never raises for a missing or
    unreadable file."""
    environ = os.environ if environ is None else environ
    if path is None:
        configured = environ.get("TESTATLAS_ENV_FILE")
        if configured == "":
            return []
        path = Path(configured) if configured else _ROOT / ".env"
    path = Path(path)
    try:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except OSError:
        return []

    loaded = []
    for key, value in parse(text).items():
        if key not in environ:
            environ[key] = value
            loaded.append(key)

    if loaded:
        print(f"TestAtlas: loaded {len(loaded)} setting(s) from {path}: {', '.join(loaded)}", file=sys.stderr)
    if mode & (stat.S_IRWXG | stat.S_IRWXO) and os.name == "posix":
        print(f"TestAtlas: warning: {path} is readable by other users; run `chmod 600 {path}`", file=sys.stderr)
    return loaded
