"""Uniform error envelope for every HTTP response produced by the API."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.responses import Response

logger = get_logger(__name__)


class ErrorBody(BaseModel):
    """Machine readable description of a failure."""

    code: str
    message: str
    details: dict[str, object] = {}


class ErrorResponse(BaseModel):
    """Envelope returned for every non-2xx API response."""

    error: ErrorBody


def _render(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object],
) -> JSONResponse:
    payload = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


async def _handle_app_error(_: Request, exc: Exception) -> Response:
    error = exc if isinstance(exc, AppError) else AppError()
    logger.warning("app_error", error_code=error.code, error_message=error.message)
    return _render(error.status_code, error.code, error.message, error.details)


async def _handle_http_exception(_: Request, exc: Exception) -> Response:
    status_code = exc.status_code if isinstance(exc, HTTPException) else 500
    detail = exc.detail if isinstance(exc, HTTPException) else "Internal server error"
    message = detail if isinstance(detail, str) else HTTPStatus(status_code).phrase
    code = HTTPStatus(status_code).name.lower()
    return _render(status_code, code, message, {})


async def _handle_validation_error(_: Request, exc: Exception) -> Response:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return _render(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "validation_error",
        "Request payload is invalid",
        {"fields": [dict(item) for item in errors]},
    )


async def _handle_unexpected_error(_: Request, exc: Exception) -> Response:
    logger.exception("unhandled_exception", exc_info=exc)
    return _render(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "internal_error",
        "Internal server error",
        {},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the project wide exception handlers to the application."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
