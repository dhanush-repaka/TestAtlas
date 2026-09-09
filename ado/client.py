"""Thin Azure DevOps REST + git client, authenticated with a Personal Access Token.

PATs are used as the password half of HTTP Basic auth (empty username), per
ADO's own convention, and are also embedded in the git remote URL for
clone/fetch. Callers must never log/print the URL built by `_clone_url` --
`clone_or_update` deliberately keeps the PAT out of subprocess output.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

API_VERSION = "7.1"
TIMEOUT = 30


class InvalidAdoUrlError(ValueError):
    pass


def parse_repo_url(url: str) -> dict:
    """Accepts an Azure DevOps HTTPS repo/clone URL and returns {"org", "project", "repo"}.

    Supported shapes (the ones ADO's own "Clone" button offers):
        https://dev.azure.com/{org}/{project}/_git/{repo}
        https://{org}@dev.azure.com/{org}/{project}/_git/{repo}   (credentials-in-url form)
        https://{org}.visualstudio.com/{project}/_git/{repo}
        https://{org}.visualstudio.com/DefaultCollection/{project}/_git/{repo}

    A trailing ".git" is stripped and URL-encoded spaces in project/repo names
    (ADO encodes these as %20) are decoded. Raises InvalidAdoUrlError with a
    message meant to be shown directly to the user.
    """
    url = (url or "").strip()
    if not url:
        raise InvalidAdoUrlError("Paste the repo's URL (from ADO's own 'Clone' button).")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidAdoUrlError(
            "That doesn't look like an HTTPS URL. Use the HTTPS clone URL from ADO's 'Clone' button "
            "(SSH URLs aren't supported here since auth is PAT-based)."
        )

    host = (parsed.hostname or "").lower()
    parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]

    if host == "dev.azure.com":
        if len(parts) >= 4 and parts[2] == "_git":
            org, project, repo = parts[0], parts[1], parts[3]
        else:
            raise InvalidAdoUrlError(
                f"Couldn't find /{{org}}/{{project}}/_git/{{repo}} in that dev.azure.com URL: {url}"
            )
    elif host.endswith(".visualstudio.com"):
        org = host.split(".visualstudio.com")[0]
        if len(parts) >= 2 and parts[0].lower() == "defaultcollection":
            parts = parts[1:]
        if len(parts) >= 3 and parts[1] == "_git":
            project, repo = parts[0], parts[2]
        else:
            raise InvalidAdoUrlError(f"Couldn't find /{{project}}/_git/{{repo}} in that visualstudio.com URL: {url}")
    else:
        raise InvalidAdoUrlError(
            "This doesn't look like an Azure DevOps repo URL. Expected something like "
            "https://dev.azure.com/{org}/{project}/_git/{repo} "
            "or https://{org}.visualstudio.com/{project}/_git/{repo}."
        )

    if repo.endswith(".git"):
        repo = repo[:-4]
    return {"org": org, "project": project, "repo": repo}


def _auth(pat: str):
    return ("", pat)


def test_connection(org: str, project: str, pat: str) -> tuple[bool, str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version={API_VERSION}"
    try:
        resp = requests.get(url, auth=_auth(pat), timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"Network error: {e}"
    if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
        names = [r["name"] for r in resp.json().get("value", [])]
        return True, f"Connected. {len(names)} repo(s) visible: {', '.join(names[:8])}"
    if resp.status_code == 401:
        return False, "401 Unauthorized -- check the PAT and its 'Code (Read)' scope."
    if "text/html" in resp.headers.get("content-type", ""):
        # ADO redirects invalid org/project (or an invalid PAT, on some tenants) to an
        # HTML sign-in page rather than a clean 401 -- surface that plainly instead of
        # dumping the markup.
        return False, (
            f"HTTP {resp.status_code}: got a sign-in page back instead of data. "
            "Double check the org and project names in the URL, and that the PAT is valid."
        )
    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def _clone_url(org: str, project: str, repo: str, pat: str) -> str:
    return f"https://{quote(pat, safe='')}@dev.azure.com/{quote(org)}/{quote(project)}/_git/{quote(repo)}"


def clone_or_update(org: str, project: str, repo: str, pat: str, branch: str, dest_dir: Path) -> None:
    """Clones into dest_dir, or fetches+resets if it's already a checkout there.

    Raises RuntimeError with the PAT scrubbed from any error text.
    """
    url = _clone_url(org, project, repo, pat)

    def _run(args: list[str], cwd: Path | None = None) -> None:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            safe_err = (result.stderr or result.stdout or "").replace(pat, "***")
            raise RuntimeError(f"git command failed: {' '.join(a if pat not in a else '<url>' for a in args)}\n{safe_err}")

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
