"""Pure ASGI middleware for request correlation and access logging."""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders

from app.core.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)

REQUEST_ID_HEADER: Final = "x-request-id"
_SERVER_ERROR_THRESHOLD: Final = 500
_CLIENT_ERROR_THRESHOLD: Final = 400


class RequestContextMiddleware:
    """Bind a request id to the log context and emit one access log per request."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER) or uuid4().hex
        method: str = scope["method"]
        path: str = scope["path"]

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            http_method=method,
            http_path=path,
        )

        status_code = _SERVER_ERROR_THRESHOLD
        started_at = perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            log = logger.info
            if status_code >= _SERVER_ERROR_THRESHOLD:
                log = logger.error
            elif status_code >= _CLIENT_ERROR_THRESHOLD:
                log = logger.warning
            log("http_request", status_code=status_code, duration_ms=duration_ms)
            structlog.contextvars.clear_contextvars()
