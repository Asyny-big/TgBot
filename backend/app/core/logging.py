"""Structured logging setup.

A single stdout stream carries every log record: JSON in production (ready for
Loki/ELK ingestion) and colourised key-value output during local development.
Standard library loggers used by uvicorn, SQLAlchemy and aiogram are routed
through the same structlog pipeline so the output stays homogeneous.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Final

import structlog

from app.core.config import LogFormat

if TYPE_CHECKING:
    from structlog.typing import Processor

_THIRD_PARTY_LOGGERS: Final = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "alembic",
    "aiogram",
    "aiogram.event",
    "httpx",
)


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def _renderer(log_format: LogFormat) -> Processor:
    if log_format is LogFormat.JSON:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=True)


def configure_logging(*, level: str, log_format: LogFormat) -> None:
    """Configure structlog and the standard library root logger.

    Idempotent: calling it twice (application factory plus bot entrypoint in the
    same process) simply replaces the root handler.
    """
    numeric_level = logging.getLevelNamesMapping()[level.upper()]
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _renderer(log_format),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = True

    # SQL statement logging is controlled by POSTGRES_ECHO, never by log level.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for the given module name."""
    return structlog.stdlib.get_logger(name)
