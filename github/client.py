"""Thin GitHub git client. Unlike ADO, a token is OPTIONAL: a public repo
clones and reads fine with no auth at all, so PAT is only needed for private
repos (a token with 'repo' scope, classic, or Contents:Read, fine-grained).

Callers must never log/print the URL built by `_clone_url` when a token is
present -- `clone_or_update` deliberately keeps it out of subprocess output.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

TIMEOUT = 30


class InvalidGithubUrlError(ValueError):
    pass


def parse_repo_url(url: str) -> dict:
    """Accepts a GitHub HTTPS repo URL and returns {"owner", "repo"}.

    Supported shapes:
        https://github.com/{owner}/{repo}
        https://github.com/{owner}/{repo}.git

    Raises InvalidGithubUrlError with a message meant to be shown directly to the user.
    """
    url = (url or "").strip()
    if not url:
        raise InvalidGithubUrlError("Paste the repo's GitHub URL (from GitHub's own 'Code' button).")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidGithubUrlError(
            "That doesn't look like an HTTPS URL. Use the HTTPS clone URL from GitHub's 'Code' "
            "button (SSH URLs aren't supported here since auth is PAT-based)."
        )
    host = (parsed.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        raise InvalidGithubUrlError(
            f"This doesn't look like a github.com URL. Expected something like "
            f"https://github.com/{{owner}}/{{repo}}. Got: {url}"
        )
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise InvalidGithubUrlError(f"Couldn't find /{{owner}}/{{repo}} in that GitHub URL: {url}")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return {"owner": owner, "repo": repo}


def _auth_headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def test_connection(owner: str, repo: str, token: str | None = None) -> tuple[bool, str]:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        resp = requests.get(url, headers=_auth_headers(token), timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"Network error: {e}"
    if resp.status_code == 200:
        data = resp.json()
        visibility = "private" if data.get("private") else "public"
        return True, f"Connected. {visibility} repo, default branch '{data.get('default_branch', 'main')}'."
    if resp.status_code == 404:
        return False, "404 Not Found -- check the owner/repo, or add a token if this is a private repo."
    if resp.status_code == 401:
        return False, "401 Unauthorized -- check the token."
    if resp.status_code == 403:
        return False, (
            "403 Forbidden -- either rate-limited (try again shortly) or the token lacks access "
            "to this repo."
        )
    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def _clone_url(owner: str, repo: str, token: str | None) -> str:
    if token:
        return f"https://{quote(token, safe='')}@github.com/{quote(owner)}/{quote(repo)}.git"
    return f"https://github.com/{quote(owner)}/{quote(repo)}.git"


def clone_or_update(owner: str, repo: str, token: str | None, branch: str, dest_dir: Path) -> None:
    """Clones into dest_dir, or fetches+resets if it's already a checkout there.

    Raises RuntimeError with the token scrubbed from any error text.
    """
    url = _clone_url(owner, repo, token)

    def _run(args: list[str], cwd: Path | None = None) -> None:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            safe_err = (result.stderr or result.stdout or "")
            if token:
                safe_err = safe_err.replace(token, "***")
            safe_args = [a if not (token and token in a) else "<url>" for a in args]
            raise RuntimeError(f"git command failed: {' '.join(safe_args)}\n{safe_err}")

    if (dest_dir / ".git").exists():
        _run(["git", "remote", "set-url", "origin", url], cwd=dest_dir)
        _run(["git", "fetch", "--depth", "1", "origin", branch], cwd=dest_dir)
        _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=dest_dir)
        _run(["git", "clean", "-fd"], cwd=dest_dir)
    else:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        # Shallow clone: only the latest commit on this branch -- the KG pipeline
        # only ever reads a checkout's current file contents, never git history,
        # and a full clone of an established repo (years of commits) can be many
        # times slower/larger for zero benefit here.
        _run(["git", "clone", "--branch", branch, "--single-branch", "--depth", "1", url, str(dest_dir)])
