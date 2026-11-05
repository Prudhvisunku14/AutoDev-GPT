"""Authentication for every API route except /health (SECURITY T7): a Bearer token, or a dashboard session cookie.

A Bearer token (the CLI, scripts, tests) works as before. A session cookie (app/core/sessions.py) works too, and a request it
authenticates that changes anything must carry `X-DevPilot-Request: 1`, which a page on another site cannot add.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from app.core.sessions import COOKIE, CSRF_HEADER, SAFE_METHODS

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def token_matches(request: Request, supplied: str) -> bool:
    expected: str = request.app.state.settings.devpilot_api_token.get_secret_value()
    return secrets.compare_digest(supplied.strip().encode(), expected.encode())


def require_api_token(request: Request) -> None:
    """FastAPI dependency. Always answers 401 (never 403) for missing or wrong credentials."""
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() == "bearer" and supplied:
        if not token_matches(request, supplied):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", _CHALLENGE)
        return
    store = getattr(request.app.state, "web_sessions", None)
    cookie = request.cookies.get(COOKIE)
    if store is not None and cookie and store.check(cookie):
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"A signed-in request that changes anything must send {CSRF_HEADER}: 1", _CHALLENGE)
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", _CHALLENGE)
