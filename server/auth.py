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
            return RedirectResponse(url=self.login_path)
        return Response(status_code=401, content="Not authenticated")
