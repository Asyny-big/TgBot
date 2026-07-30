"""FastAPI application factory for the admin API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.deps import CONTAINER_STATE_KEY, RESOURCES_STATE_KEY
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger
from app.core.resources import Resources

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create infrastructure pools on start-up and release them on shutdown."""
    settings: Settings = app.state.settings
    resources = Resources.create(settings)
    setattr(app.state, RESOURCES_STATE_KEY, resources)
    setattr(app.state, CONTAINER_STATE_KEY, Container.create(resources))

    checks = await resources.check()
    logger.info(
        "api_started",
        environment=settings.app.environment.value,
        version=__version__,
        **checks,
    )
    try:
        yield
    finally:
        await resources.close()
        logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fully configured FastAPI application."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.app.log_level, log_format=resolved.app.log_format)

    docs_enabled = resolved.app.docs_enabled and not resolved.app.environment.is_production
    app = FastAPI(
        title=resolved.app.name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = resolved

    app.add_middleware(RequestContextMiddleware)
    if resolved.security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.security.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=resolved.app.api_prefix)
    return app
