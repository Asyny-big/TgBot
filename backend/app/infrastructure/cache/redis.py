"""Redis connection wrapper.

Redis is used for idempotency locks around payment delivery and for short lived
caches. The wrapper keeps a single connection pool per process and exposes the
raw client for the higher layers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import RedisSettings

logger = get_logger(__name__)


class RedisClient:
    """Owns the Redis connection pool for the current process."""

    def __init__(self, settings: RedisSettings) -> None:
        self._client: Redis = Redis.from_url(
            settings.dsn,
            max_connections=settings.max_connections,
            socket_timeout=settings.socket_timeout,
            socket_connect_timeout=settings.socket_timeout,
            decode_responses=True,
            health_check_interval=30,
        )

    @property
    def client(self) -> Redis:
        return self._client

    async def ping(self) -> bool:
        """Return ``True`` when Redis answers PING."""
        try:
            await self._client.ping()
        except (RedisError, OSError) as error:
            logger.warning("redis_ping_failed", error=str(error))
            return False
        return True

    async def close(self) -> None:
        """Release every pooled connection."""
        await self._client.aclose()
        logger.info("redis_pool_closed")
