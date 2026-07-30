"""Infrastructure tests exercising the real Database and Redis wrappers.

Unreachable endpoints are used on purpose: no external service is required, and
the failure paths that keep the readiness probe honest are covered for real.
"""

from __future__ import annotations

from http import HTTPStatus

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import PostgresSettings, RedisSettings
from app.core.resources import Resources
from app.infrastructure.cache.redis import RedisClient
from app.infrastructure.db.engine import Database
from app.main import create_app
from tests.settings_factory import build_settings

UNREACHABLE_PORT = 1


def _unreachable_postgres() -> PostgresSettings:
    return PostgresSettings(
        host="127.0.0.1",
        port=UNREACHABLE_PORT,
        user="tgshop",
        password=SecretStr("tgshop"),
        db="tgshop",
        pool_timeout=1.0,
    )


def _unreachable_redis() -> RedisSettings:
    return RedisSettings(host="127.0.0.1", port=UNREACHABLE_PORT, socket_timeout=1.0)


async def test_database_ping_reports_failure_without_raising() -> None:
    database = Database(_unreachable_postgres())
    try:
        assert await database.ping() is False
    finally:
        await database.dispose()


async def test_database_builds_an_asyncpg_engine() -> None:
    database = Database(_unreachable_postgres())
    try:
        assert database.engine.url.drivername == "postgresql+asyncpg"
        assert database.engine.url.database == "tgshop"
        assert database.session_factory.kw["expire_on_commit"] is False
    finally:
        await database.dispose()


async def test_redis_ping_reports_failure_without_raising() -> None:
    cache = RedisClient(_unreachable_redis())
    try:
        assert await cache.ping() is False
    finally:
        await cache.close()


async def test_resources_check_and_close_are_idempotent() -> None:
    settings = build_settings(postgres=_unreachable_postgres(), redis=_unreachable_redis())
    resources = Resources.create(settings)
    assert await resources.check() == {"database": False, "redis": False}
    await resources.close()
    await resources.close()


async def test_lifespan_creates_real_resources_and_reports_degraded() -> None:
    """Full application boot: the lifespan wires real pools, the probe answers 503."""
    settings = build_settings(postgres=_unreachable_postgres(), redis=_unreachable_redis())
    app = create_app(settings)

    transport = ASGITransport(app=app)
    # Entering lifespan_context runs the real start-up/shutdown callbacks.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["components"] == {"database": "fail", "redis": "fail"}
