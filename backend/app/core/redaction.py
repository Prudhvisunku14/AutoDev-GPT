"""Secret redaction for logs, events, DB rows and tool results (SECURITY T6).

Two layers:
1. Known values: every configured secret (from Settings) is masked wherever it appears.
2. Patterns: common token formats and `key=value` pairs with sensitive key names,
   so secrets that never passed through Settings are still caught.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "[REDACTED]"

# Shorter known values are not masked by substring: they would destroy ordinary text.
MIN_KNOWN_SECRET_LENGTH = 8

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI keys, including sk-proj-
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub classic and app tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PATs
    re.compile(r"ya29\.[0-9A-Za-z\-_]{20,}"),  # Google OAuth access tokens
    re.compile(r"1//[0-9A-Za-z\-_]{20,}"),  # Google OAuth refresh tokens
    re.compile(r"(?i)(?<=bearer )[A-Za-z0-9\-._~+/]{8,}=*"),  # Authorization: Bearer <x>
    re.compile(r"(?i)(?<=basic )[A-Za-z0-9+/]{8,}=*"),  # Authorization: Basic <x>
)

_SENSITIVE_KEY = r"[A-Za-z0-9_\-]*(?:api[_\-]?key|secret|password|passwd|token|authorization)"
_KEY_VALUE_PATTERN = re.compile(
    rf"(?i)(\b{_SENSITIVE_KEY}\b\s*[:=]\s*)(['\"]?)([^\s'\",;&]+)"
)
_SENSITIVE_KEY_NAME = re.compile(rf"(?i)^{_SENSITIVE_KEY}$")


class Redactor:
    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        # Longest first, so a secret that contains another is masked whole.
        self._known = sorted(
            {s for s in known_secrets if len(s) >= MIN_KNOWN_SECRET_LENGTH},
            key=len,
            reverse=True,
        )

    def redact_text(self, text: str) -> str:
        for secret in self._known:
            text = text.replace(secret, MASK)
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(MASK, text)
        return _KEY_VALUE_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)

    def redact(self, value: Any) -> Any:
        """Recursively redact strings inside dicts, lists and tuples."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                k: (MASK if _is_sensitive_key(k) and value[k] not in (None, "") else self.redact(value[k]))
                for k in value
            }
        if isinstance(value, list | tuple):
            return type(value)(self.redact(v) for v in value)
        return value


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_KEY_NAME.match(key))
