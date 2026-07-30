"""Redis fixed-window rate limiting.

Used to slow down credential guessing on the admin login. The counter is created
with an expiry on its first hit, so a burst can never leave a key behind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)

RATE_LIMIT_KEY_PREFIX: Final = "rate:"


class RedisRateLimiter:
    """Counts attempts per key inside a fixed time window."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def hit(self, key: str, *, limit: int, window_seconds: float) -> bool:
        """Register an attempt; return ``True`` while the caller is under the limit."""
        name = f"{RATE_LIMIT_KEY_PREFIX}{key}"
        pipeline = self._client.pipeline()
        pipeline.incr(name)
        pipeline.pttl(name)
        attempts, ttl = await pipeline.execute()

        if int(ttl) < 0:
            # First hit in this window (or a key without an expiry): set one.
            await self._client.pexpire(name, max(1, int(window_seconds * 1000)))

        allowed = int(attempts) <= limit
        if not allowed:
            logger.warning("rate_limit_exceeded", rate_key=key, attempts=int(attempts))
        return allowed

    async def reset(self, key: str) -> None:
        """Forget the attempts for a key (called after a successful login)."""
        await self._client.delete(f"{RATE_LIMIT_KEY_PREFIX}{key}")
