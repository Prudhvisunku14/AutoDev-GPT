from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from app.core.logging import RedactingJsonFormatter, configure_logging
from app.core.redaction import MASK, Redactor

SECRET = "known-secret-value-123"


@pytest.fixture
def capture() -> tuple[logging.Logger, io.StringIO]:
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(RedactingJsonFormatter(Redactor([SECRET])))
    logger = logging.getLogger("test.devpilot.logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, buffer


def _lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_records_are_json_with_standard_fields(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, buffer = capture
    logger.info("hello %s", "world", extra={"run_id": "r1", "color_message": "\x1b[36mhello\x1b[0m"})
    (entry,) = _lines(buffer)
    assert entry["msg"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.devpilot.logging"
    assert entry["extra"] == {"run_id": "r1"}
    assert entry["ts"].endswith("+00:00")


def test_secrets_are_redacted_in_message_args_extras_and_tracebacks(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, buffer = capture
    logger.info("msg %s", SECRET, extra={"detail": f"x {SECRET}", "password": "pw123456"})
    try:
        raise RuntimeError(f"failed with {SECRET}")
    except RuntimeError:
        logger.exception("boom")
    raw = buffer.getvalue()
    assert SECRET not in raw
    assert "pw123456" not in raw
    entries = _lines(buffer)
    assert entries[0]["extra"]["password"] == MASK
    assert "RuntimeError" in entries[1]["exc"]


def test_configure_logging_is_idempotent() -> None:
    redactor = Redactor()
    configure_logging("INFO", redactor)
    configure_logging("DEBUG", redactor)
    root = logging.getLogger()
    assert sum(1 for h in root.handlers if h.get_name() == "devpilot-json") == 1
    assert root.level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").handlers == []
