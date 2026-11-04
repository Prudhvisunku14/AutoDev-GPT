"""HMAC signing for n8n webhooks and callbacks (ARCHITECTURE §8.1, SECURITY T8).

The signature is `sha256=` + hex `HMAC-SHA256(N8N_WEBHOOK_SECRET, timestamp + "." + body)`, sent in
`X-DevPilot-Signature`, with the Unix timestamp in `X-DevPilot-Timestamp`. A callback is accepted only with a valid
signature, a timestamp within five minutes of now (either side) and a signature not seen before within that window.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import OrderedDict

TIMESTAMP_HEADER = "X-DevPilot-Timestamp"
SIGNATURE_HEADER = "X-DevPilot-Signature"
WINDOW_SECONDS = 300
_PREFIX = "sha256="


class SignatureRejected(Exception):
    """The request is unsigned, wrongly signed, stale or a replay. The reason never includes the secret."""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def signed_headers(secret: str, body: bytes, *, now: float | None = None) -> dict[str, str]:
    timestamp = int(now if now is not None else time.time())
    return {TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: sign(secret, timestamp, body)}


class ReplayGuard:
    """Remembers accepted signatures until they fall out of the window, so the same signed request is accepted once."""

    def __init__(self, *, window: int = WINDOW_SECONDS, max_entries: int = 10_000) -> None:
        self._window = window
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def first_use(self, signature: str, now: float) -> bool:
        with self._lock:
            while self._seen and next(iter(self._seen.values())) < now - 2 * self._window:
                self._seen.popitem(last=False)
            if signature in self._seen:
                return False
            if len(self._seen) >= self._max:  # fail closed rather than forget a signature still inside its window
                return False
            self._seen[signature] = now
            return True


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes, *, guard: ReplayGuard, now: float | None = None, window: int = WINDOW_SECONDS) -> None:
    if not timestamp or not signature:
        raise SignatureRejected("the request is not signed")
    if not timestamp.isdigit() or len(timestamp) > 12:
        raise SignatureRejected("the timestamp is not a Unix time")
    current = now if now is not None else time.time()
    if abs(current - int(timestamp)) > window:
        raise SignatureRejected("the timestamp is outside the five-minute window")
    if not hmac.compare_digest(sign(secret, int(timestamp), body), signature.strip()):
        raise SignatureRejected("the signature does not match")
    if not guard.first_use(signature.strip(), current):
        raise SignatureRejected("this signed request was already received")
