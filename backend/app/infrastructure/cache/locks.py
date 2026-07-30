"""Redis backed distributed locks with a mandatory expiry.

Every lock is written with ``SET key token NX PX ttl``:

* ``NX`` makes acquisition atomic — exactly one holder wins a race.
* ``PX`` guarantees the lock disappears on its own. A process that crashes
  between acquiring and releasing cannot block a buyer forever.

Release is a compare-and-delete executed server side, so a holder whose TTL
already expired can never delete a lock that now belongs to somebody else.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from app.core.exceptions import LockBusyError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

    from app.core.config import RedisSettings

logger = get_logger(__name__)

LOCK_KEY_PREFIX: Final = "lock:"
_RETRY_DELAY_SECONDS: Final = 0.05

# Delete the key only when it still holds our token.
_RELEASE_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisLockManager:
    """Hands out short lived locks backed by Redis."""

    def __init__(self, client: Redis, settings: RedisSettings) -> None:
        self._client = client
        self._default_ttl = settings.lock_ttl_seconds
        self._default_wait = settings.lock_wait_seconds
        self._release = client.register_script(_RELEASE_SCRIPT)

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        wait_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        """Hold ``key`` for at most ``ttl_seconds``, then release it.

        Raises:
            LockBusyError: the lock stayed busy for the whole waiting window.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        wait = wait_seconds if wait_seconds is not None else self._default_wait
        name = f"{LOCK_KEY_PREFIX}{key}"
        token = uuid4().hex

        if not await self._acquire(name, token, ttl=ttl, wait=wait):
            logger.warning("lock_busy", lock_key=key, waited_seconds=wait)
            raise LockBusyError(lock_key=key)

        try:
            yield
        finally:
            await self._release_safely(name, token, key)

    async def _acquire(self, name: str, token: str, *, ttl: float, wait: float) -> bool:
        deadline = monotonic() + wait
        while True:
            # At least one millisecond: px=0 would be rejected by Redis.
            acquired = await self._client.set(name, token, nx=True, px=max(1, int(ttl * 1000)))
            if acquired:
                return True
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

    async def _release_safely(self, name: str, token: str, key: str) -> None:
        released = await self._release(keys=[name], args=[token])
        if not released:
            # The TTL fired before we finished: the work took longer than the
            # lock's lifetime. Worth knowing about — it means the TTL is too low.
            logger.warning("lock_expired_before_release", lock_key=key)

    async def ttl(self, key: str) -> int:
        """Remaining lifetime of a lock in milliseconds (diagnostics only)."""
        return int(await self._client.pttl(f"{LOCK_KEY_PREFIX}{key}"))
