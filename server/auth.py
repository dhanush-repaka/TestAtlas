"""A minimal password gate for hosting TestAtlas somewhere reachable on the
internet. Single shared password (set via the TESTATLAS_PASSWORD env var),
one signed session cookie -- this is deliberately not a real user system
(no per-user accounts, no rate limiting beyond what your reverse proxy does).
Good enough for "keep this off the open internet"; not a substitute for real
auth if this ever needs to support more than one person.

If TESTATLAS_PASSWORD isn't set, auth is disabled and a loud warning is
logged at startup -- that's the local-dev default; a production deploy
should always set it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "ta_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _secret() -> str:
    # Falls back to the password itself if no separate secret is set -- fine
    # for a single-shared-password gate; only the password's holder can forge
    # a valid cookie either way.
    return os.environ.get("TESTATLAS_SECRET") or os.environ.get("TESTATLAS_PASSWORD", "")


def password_configured() -> bool:
    return bool(os.environ.get("TESTATLAS_PASSWORD"))


def check_password(candidate: str) -> bool:
    expected = os.environ.get("TESTATLAS_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(candidate, expected)


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    expires_at = str(int(time.time()) + SESSION_TTL_SECONDS)
    return f"{expires_at}.{_sign(expires_at)}"


def verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires_at, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _sign(expires_at)):
        return False
    try:
        return int(expires_at) > time.time()
    except ValueError:
        return False


def safe_next_path(raw: str | None, base_path: str) -> str:
    """Where to send someone after they log in -- the deep link they actually
    asked for (a shared repo/tab URL, a bookmark) rather than always the
    dashboard. Only ever a path inside this app: must start with the app's own
    root ("{base_path}/"), ruling out a scheme ("https://evil") and a
    protocol-relative host ("//evil.com", which also starts with "/" -- an
    unauthenticated visitor's `next` is attacker-controlled input, and honoring
    it blindly would let a crafted login link redirect a fresh session
    anywhere). Falls back to the app root for anything else, including empty."""
    root = f"{base_path}/"
    if raw and raw.startswith(root) and not raw.startswith("//"):
        return raw
    return root


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirects (pages) or 401s (everything else) any request without a
    valid session cookie, except the login page and its own form submission.
    A no-op entirely when TESTATLAS_PASSWORD isn't set."""

    def __init__(self, app, base_path: str = ""):
        super().__init__(app)
        self.login_path = f"{base_path}/login"

    async def dispatch(self, request: Request, call_next):
        if not password_configured():
            return await call_next(request)

        if request.url.path == self.login_path:
            return await call_next(request)

        if verify_session_token(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)

        accepts_html = "text/html" in request.headers.get("accept", "")
        if accepts_html and request.method == "GET":
            # Carry the page they actually asked for through the login form, so a
            # shared deep link (a specific repo/tab) lands there after signing in
            # instead of always dropping back to the dashboard. (request.url.path
            # is never self.login_path here -- that case already returned above.)
            return RedirectResponse(url=f"{self.login_path}?next={quote(request.url.path, safe='')}")
        return Response(status_code=401, content="Not authenticated")
