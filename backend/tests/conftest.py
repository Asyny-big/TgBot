"""Shared pytest fixtures.

Settings are built explicitly instead of being read from the environment so the
suite is deterministic regardless of the developer's local ``.env``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.deps import RESOURCES_STATE_KEY, get_resources
from app.core.config import (
    AppSettings,
    CryptoBotSettings,
    DeliverySettings,
    Environment,
    LogFormat,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from app.core.resources import Resources
from app.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

# Database fixtures live in tests/db.py; re-exported here so pytest collects them.
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

VALID_BOT_TOKEN = "123456789:AAHfake-Test-Token_for_unit_tests_only01"  # noqa: S105

_SETTINGS_CLASSES = (
    AppSettings,
    DeliverySettings,
    PostgresSettings,
    RedisSettings,
    TelegramSettings,
    CryptoBotSettings,
    SecuritySettings,
)


@pytest.fixture(autouse=True)
def _ignore_local_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic: a developer's .env must never leak into tests."""
    for settings_class in _SETTINGS_CLASSES:
        monkeypatch.setitem(settings_class.model_config, "env_file", None)


def build_settings(**overrides: Any) -> Settings:
    """Return a valid settings object; ``overrides`` replaces whole groups."""
    groups: dict[str, Any] = {
        "app": AppSettings(
            environment=Environment.TESTING,
            debug=False,
            log_level="INFO",
            log_format=LogFormat.CONSOLE,
            docs_enabled=True,
        ),
        "postgres": PostgresSettings(
            host="localhost",
            user="shop",
            password=SecretStr("shop-password"),
            db="shop",
        ),
        "redis": RedisSettings(host="localhost"),
        "telegram": TelegramSettings(
            bot_token=SecretStr(VALID_BOT_TOKEN),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        ),
        "cryptobot": CryptoBotSettings(
            api_token=SecretStr("12345:cryptobot-test-token"),
            network="testnet",
        ),
        "delivery": DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
        "security": SecuritySettings(
            jwt_secret=SecretStr("a" * 48),
            admin_username="administrator",
            admin_password=SecretStr("super-secret-password"),
            cors_origins=("http://localhost:5173",),
        ),
    }
    groups.update(overrides)
    return Settings(**groups)


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
