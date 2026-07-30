"""Shared pytest fixtures.

Settings are built explicitly instead of being read from the environment so the
suite is deterministic regardless of the developer's local ``.env``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import RESOURCES_STATE_KEY, get_resources
from app.core.resources import Resources
from app.main import create_app
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from app.core.config import Settings

# Database fixtures live in tests/db.py; re-exported here so pytest collects them.
from tests.api_harness import admin_api, api  # noqa: F401  (fixture re-export)
from tests.db import (  # noqa: F401  (fixture re-export)
    database_dsn,
    db_session,
    live_database,
    live_locks,
    live_uow_factory,
    migrated_database,
    products,
    purchases,
    redis_client,
    stats,
    users,
)


class FakeDatabase:
    """Database stand-in whose ping result is controlled by the test."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.disposed = False

    async def ping(self) -> bool:
        return self.healthy

    async def dispose(self) -> None:
        self.disposed = True


class FakeCache:
    """Redis stand-in whose ping result is controlled by the test."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.closed = False

    async def ping(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        self.closed = True


def build_resources(
    settings: Settings,
    *,
    database_healthy: bool = True,
    cache_healthy: bool = True,
) -> Resources:
    """Build a resources bundle backed by in-memory fakes."""
    return Resources(
        settings=settings,
        database=FakeDatabase(healthy=database_healthy),  # type: ignore[arg-type]
        cache=FakeCache(healthy=cache_healthy),  # type: ignore[arg-type]
    )


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Application instance with the lifespan bypassed by injected fakes."""
    application = create_app(settings)
    resources = build_resources(settings)
    setattr(application.state, RESOURCES_STATE_KEY, resources)
    application.dependency_overrides[get_resources] = lambda: resources
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
