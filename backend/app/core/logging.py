"""Structured JSON logging with secret redaction on every record."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.redaction import Redactor

# Attributes every LogRecord has; anything else was passed through `extra=`.
# uvicorn adds `color_message` (the message with ANSI colors), which is noise in JSON.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime", "color_message"}
)

_HANDLER_NAME = "devpilot-json"


class RedactingJsonFormatter(logging.Formatter):
    """Formats each record as one JSON line, redacting message, extras and tracebacks."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if extras:
            entry["extra"] = extras
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(self._redactor.redact(entry), default=str, ensure_ascii=False)


def configure_logging(level: str, redactor: Redactor) -> None:
    """Install one JSON handler on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(RedactingJsonFormatter(redactor))
    root.addHandler(handler)
    root.setLevel(level)
    # Route uvicorn's loggers through the same redacting handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
