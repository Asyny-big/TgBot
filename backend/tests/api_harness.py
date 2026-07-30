"""Admin API harness.

The application is exercised through HTTP against the live database and live
Redis; only the Telegram transport and the Crypto Pay HTTP API are replaced.
Authentication, validation, error envelopes and dependency wiring are all real.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from app.api.deps import CHECKOUT_STATE_KEY, CONTAINER_STATE_KEY, RESOURCES_STATE_KEY
from app.core.config import (
    BotSettings,
    CryptoBotSettings,
    DeliverySettings,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from app.core.container import Container
from app.infrastructure.cache.rate_limit import RedisRateLimiter
from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
from app.infrastructure.payments.cryptobot import CryptoBotClient
from app.infrastructure.telegram.factory import create_delivery_gateway, create_stars_sender
from app.main import create_app
from app.services.auth import AuthService
from app.services.products import ProductService
from app.services.purchases import PurchaseService
from app.services.stats import StatsService
from tests.bot_harness import RecordingBot
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from redis.asyncio import Redis

    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from app.services.checkout import CheckoutService

ADMIN_USERNAME = "administrator"
ADMIN_PASSWORD = "super-secret-password"  # noqa: S105
CRYPTO_TOKEN = "12345:cryptobot-test-token"  # noqa: S105


def api_settings(dsn: str, **overrides: Any) -> Settings:
    """Settings pointing at the live test infrastructure."""
    url = httpx.URL(dsn.replace("postgresql+asyncpg", "postgresql"))
    defaults: dict[str, Any] = {
        "postgres": PostgresSettings(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=SecretStr(url.password),
            db=url.path.lstrip("/"),
        ),
        "redis": RedisSettings(host="127.0.0.1", lock_ttl_seconds=10.0),
        "telegram": TelegramSettings(
            bot_token=SecretStr("123456789:AAHfake-Test-Token_for_unit_tests_only01"),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        ),
        "cryptobot": CryptoBotSettings(api_token=SecretStr(CRYPTO_TOKEN), network="testnet"),
        "bot": BotSettings(throttle_seconds=0.0),
        "delivery": DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
        "security": SecuritySettings(
            jwt_secret=SecretStr("a" * 48),
            admin_username=ADMIN_USERNAME,
            admin_password=SecretStr(ADMIN_PASSWORD),
            cookie_secure=False,
            login_rate_limit=5,
            login_rate_window_seconds=60.0,
        ),
    }
    defaults.update(overrides)
    return build_settings(**defaults)


@dataclass(frozen=True, slots=True)
class ApiHarness:
    """An authenticated (or not) HTTP client plus the wired services behind it."""

    app: FastAPI
    client: httpx.AsyncClient
    container: Container
    checkout: CheckoutService
    bot: RecordingBot
    settings: Settings

    @property
    def prefix(self) -> str:
        return self.settings.app.api_prefix

    async def login(self) -> str:
        """Log in and return the access token."""
        response = await self.client.post(
            f"{self.prefix}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == httpx.codes.OK, response.text
        return str(response.json()["access_token"])

    async def authenticate(self) -> None:
        """Attach an Authorization header to every later request."""
        token = await self.login()
        self.client.headers["Authorization"] = f"Bearer {token}"


def build_api_harness(
    *,
    settings: Settings,
    database: Database,
    locks: RedisLockManager,
    redis: Redis,
    crypto_transport: httpx.MockTransport | None = None,
) -> tuple[ApiHarness, httpx.AsyncClient]:
    """Wire the application over the live infrastructure and a fake provider."""
    uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    crypto_client = CryptoBotClient(
        settings.cryptobot,
        client=httpx.AsyncClient(
            base_url=settings.cryptobot.api_base_url,
            transport=crypto_transport
            or httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True, "result": {}})),
        ),
    )
    from app.infrastructure.cache.revocation import RedisTokenRevocationStore  # noqa: PLC0415

    container = Container(
        settings=settings,
        uow_factory=uow_factory,
        locks=locks,
        rate_limiter=RedisRateLimiter(redis),
        crypto_payments=crypto_client,
        products=ProductService(uow_factory=uow_factory, telegram=settings.telegram),
        purchases=PurchaseService(uow_factory=uow_factory, locks=locks),
        stats=StatsService(uow_factory=uow_factory),
        auth=AuthService(settings.security, RedisTokenRevocationStore(redis)),
    )
    bot = RecordingBot()
    checkout = container.build_checkout(
        delivery_gateway=create_delivery_gateway(bot),
        stars=create_stars_sender(bot),
    )

    app = create_app(settings)
    # The lifespan builds its own resources; tests inject the live ones instead.
    setattr(app.state, RESOURCES_STATE_KEY, _ResourcesStub(settings))
    setattr(app.state, CONTAINER_STATE_KEY, container)
    setattr(app.state, CHECKOUT_STATE_KEY, checkout)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
    )
    harness = ApiHarness(
        app=app,
        client=client,
        container=container,
        checkout=checkout,
        bot=bot,
        settings=settings,
    )
    return harness, client


@dataclass(frozen=True, slots=True)
class _ResourcesStub:
    """Only ``settings`` is read from the resources bundle by the API."""

    settings: Settings


@pytest.fixture
async def api(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> AsyncIterator[ApiHarness]:
    """An unauthenticated admin API client."""
    settings = api_settings(migrated_database)
    harness, client = build_api_harness(
        settings=settings,
        database=live_database,
        locks=live_locks,
        redis=redis_client,
    )
    try:
        yield harness
    finally:
        await client.aclose()
        await harness.bot.session.close()


@pytest.fixture
async def admin_api(api: ApiHarness) -> ApiHarness:
    """An admin API client that is already logged in."""
    await api.authenticate()
    return api
