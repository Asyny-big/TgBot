"""Application level exception hierarchy.

Errors carry an HTTP status and a stable machine readable ``code`` so that both
the admin SPA and the bot can react to them without string matching.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base class for every deliberate, expected application failure."""

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(self, message: str | None = None, /, **details: object) -> None:
        self.message = message or self.message
        self.details: dict[str, object] = dict(details)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


class ServiceUnavailableError(AppError):
    """A required infrastructure dependency is not reachable."""

    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service temporarily unavailable"
