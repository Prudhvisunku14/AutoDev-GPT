"""Bearer-token authentication for every API route except /health (SECURITY T7)."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def require_api_token(request: Request) -> None:
    """FastAPI dependency. Always answers 401 (never 403) for a missing or wrong token."""
    expected: str = request.app.state.settings.devpilot_api_token.get_secret_value()
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", _CHALLENGE)
    if not secrets.compare_digest(supplied.strip().encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", _CHALLENGE)
