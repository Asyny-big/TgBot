"""Logging pipeline tests."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import structlog

from app.core.config import LogFormat
from app.core.logging import configure_logging, get_logger

if TYPE_CHECKING:
    import pytest


def test_json_logging_emits_single_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)
    get_logger("test").info("payment_captured", provider="stars", amount=100)

    stdout = capsys.readouterr().out.strip()
    assert stdout.count("\n") == 0

    record = json.loads(stdout)
    assert record["event"] == "payment_captured"
    assert record["provider"] == "stars"
    assert record["amount"] == 100
    assert record["level"] == "info"
    assert record["logger"] == "test"
    assert record["timestamp"].endswith("Z")


def test_contextvars_are_merged_into_records(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="abc123")
    try:
        get_logger("test").info("handled")
    finally:
        structlog.contextvars.clear_contextvars()

    record = json.loads(capsys.readouterr().out.strip())
    assert record["request_id"] == "abc123"


def test_stdlib_loggers_are_routed_through_structlog(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON)
    logging.getLogger("uvicorn.error").warning("legacy message")

    record = json.loads(capsys.readouterr().out.strip())
    assert record["event"] == "legacy message"
    assert record["level"] == "warning"
    assert record["logger"] == "uvicorn.error"


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)
    assert len(logging.getLogger().handlers) == 1

    get_logger("test").info("console_event")
    assert "console_event" in capsys.readouterr().out


def test_log_level_filters_lower_severity(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", log_format=LogFormat.JSON)
    logger = get_logger("test")
    logger.info("suppressed")
    logger.warning("emitted")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "emitted"
