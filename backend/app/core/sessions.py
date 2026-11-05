"""Dashboard sessions: the browser holds an HttpOnly cookie, never the API token (SECURITY.md T7).

- **Signing in:** with the API token once (`POST /api/v1/session`), or with a one-time login link the launcher prints, so
  nobody has to paste anything. A link's code is random, works once and expires after `LINK_TTL` seconds.
- **The cookie:** random 32 bytes, `HttpOnly` (page scripts cannot read it), `SameSite=Strict`, `Secure` over https. Only its
  SHA-256 is stored. It ends after `IDLE` without use, and after `MAX_AGE` in any case; "remember" makes it outlive the
  browser window, otherwise it is a browser-session cookie.
- **Cross-site requests:** a request authenticated by the cookie that changes anything must carry `X-DevPilot-Request: 1`.
  A page on another site cannot add that header without CORS approval, and CORS allows only the configured dashboard
  origins. Bearer tokens (the CLI, scripts) are unaffected.
- **Signing out** deletes the session. Changing DEVPILOT_API_TOKEN does not end sessions; signing out everywhere does.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import WebSession

COOKIE = "devpilot_session"
CSRF_HEADER = "X-DevPilot-Request"
IDLE = timedelta(hours=12)
MAX_AGE = timedelta(days=7)
LINK_TTL = 600.0
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_FAILURES = 10  # wrong tokens per minute before sign-in is refused for the rest of that minute


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class SessionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._links: dict[str, float] = {}
        self._failures: list[float] = []
        self._guard = threading.Lock()

    # --- Sessions ---------------------------------------------------------------------------------------------------

    def create(self, *, remember: bool) -> str:
        raw = secrets.token_urlsafe(32)
        now = _now()
        with self._sessions.begin() as s:
            s.add(WebSession(id_hash=_digest(raw), created_at=now, last_used_at=now, expires_at=now + MAX_AGE, remember=remember))
        return raw

    def check(self, raw: str | None) -> bool:
        if not raw or len(raw) > 200:
            return False
        now = _now()
        with self._sessions.begin() as s:
            row = s.get(WebSession, _digest(raw))
            if row is None:
                return False
            if row.expires_at <= now or row.last_used_at + IDLE <= now:
                s.delete(row)
                return False
            row.last_used_at = now
            return True

    def end(self, raw: str | None) -> None:
        if raw:
            with self._sessions.begin() as s:
                s.execute(delete(WebSession).where(WebSession.id_hash == _digest(raw)))

    def end_all(self) -> int:
        with self._sessions.begin() as s:
            count = len(s.scalars(select(WebSession.id_hash)).all())
            s.execute(delete(WebSession))
        return count

    # --- One-time login links ---------------------------------------------------------------------------------------

    def issue_link(self) -> str:
        code = secrets.token_urlsafe(24)
        with self._guard:
            now = time.monotonic()
            self._links = {k: v for k, v in self._links.items() if v > now}
            self._links[_digest(code)] = now + LINK_TTL
        return code

    def redeem_link(self, code: str | None) -> bool:
        if not code or len(code) > 200:
            return False
        with self._guard:
            expires = self._links.pop(_digest(code), None)  # used once, whatever follows
        return expires is not None and expires > time.monotonic()

    # --- Brute force ------------------------------------------------------------------------------------------------

    def locked_out(self) -> bool:
        with self._guard:
            now = time.monotonic()
            self._failures = [t for t in self._failures if t > now - 60]
            return len(self._failures) >= MAX_FAILURES

    def failed(self) -> None:
        with self._guard:
            self._failures.append(time.monotonic())
