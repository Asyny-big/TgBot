"""Redis lock tests against a real Redis server.

The properties that matter in production are checked here: exclusivity, the
mandatory TTL, self-healing after a crash, safe release and waiting.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from app.core.config import RedisSettings
from app.core.exceptions import LockBusyError
from app.infrastructure.cache.locks import LOCK_KEY_PREFIX, RedisLockManager

if TYPE_CHECKING:
    from redis.asyncio import Redis

TTL = 0.5


@pytest.fixture
def locks(redis_client: Redis) -> RedisLockManager:
    settings = RedisSettings(lock_ttl_seconds=5.0, lock_wait_seconds=0.0)
    return RedisLockManager(redis_client, settings)


async def test_only_one_holder_at_a_time(locks: RedisLockManager) -> None:
    async with locks.lock("resource"):
        with pytest.raises(LockBusyError) as error:
            async with locks.lock("resource"):
                pass  # unreachable: acquisition raises
        assert error.value.details["lock_key"] == "resource"


async def test_the_lock_is_released_after_the_block(locks: RedisLockManager) -> None:
    async with locks.lock("resource"):
        pass
    async with locks.lock("resource"):
        pass  # a second acquisition proves the first one was released


async def test_the_lock_is_released_even_when_the_body_raises(locks: RedisLockManager) -> None:
    boom = RuntimeError("the critical section blew up")
    with pytest.raises(RuntimeError):
        async with locks.lock("resource"):
            raise boom
    async with locks.lock("resource"):
        pass


async def test_the_lock_carries_a_ttl(locks: RedisLockManager) -> None:
    async with locks.lock("resource", ttl_seconds=30):
        remaining = await locks.ttl("resource")
    assert 0 < remaining <= 30_000


async def test_an_abandoned_lock_expires_on_its_own(
    locks: RedisLockManager,
    redis_client: Redis,
) -> None:
    """Simulates a crashed holder: the key must vanish without anyone releasing it."""
    await redis_client.set(f"{LOCK_KEY_PREFIX}resource", "other-holder", px=int(TTL * 1000))

    with pytest.raises(LockBusyError):
        async with locks.lock("resource"):
            pass  # unreachable: acquisition raises

    await asyncio.sleep(TTL + 0.15)
    async with locks.lock("resource"):
        pass


async def test_releasing_does_not_delete_a_lock_owned_by_somebody_else(
    locks: RedisLockManager,
    redis_client: Redis,
) -> None:
    """A holder whose TTL already expired must not delete the next holder's lock."""
    key = f"{LOCK_KEY_PREFIX}resource"
    async with locks.lock("resource", ttl_seconds=5):
        # Somebody else took over the key while we were working.
        await redis_client.set(key, "new-holder")
    assert await redis_client.get(key) == "new-holder"


async def test_waiting_acquires_the_lock_once_it_frees_up(locks: RedisLockManager) -> None:
    order: list[str] = []

    async def holder() -> None:
        async with locks.lock("resource", ttl_seconds=5):
            order.append("holder-acquired")
            await asyncio.sleep(0.2)
        order.append("holder-released")

    async def waiter() -> None:
        await asyncio.sleep(0.05)
        async with locks.lock("resource", ttl_seconds=5, wait_seconds=2.0):
            order.append("waiter-acquired")

    await asyncio.gather(holder(), waiter())
    assert order == ["holder-acquired", "holder-released", "waiter-acquired"]


async def test_waiting_gives_up_after_the_window(locks: RedisLockManager) -> None:
    async with locks.lock("resource", ttl_seconds=5):
        with pytest.raises(LockBusyError):
            async with locks.lock("resource", wait_seconds=0.15):
                pass  # unreachable: acquisition raises


async def test_different_keys_do_not_block_each_other(locks: RedisLockManager) -> None:
    async with locks.lock("first"), locks.lock("second"):
        pass


async def test_concurrent_contenders_are_serialised(locks: RedisLockManager) -> None:
    """Twenty coroutines, one critical section: no interleaving is allowed."""
    inside = 0
    peak = 0
    winners = 0

    async def contend() -> None:
        nonlocal inside, peak, winners
        try:
            async with locks.lock("resource", ttl_seconds=5, wait_seconds=3.0):
                winners += 1
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.01)
                inside -= 1
        except LockBusyError:  # pragma: no cover — the wait window is generous
            pass

    await asyncio.gather(*(contend() for _ in range(20)))
    assert peak == 1
    assert winners == 20


async def test_revoked_tokens_are_remembered_until_they_expire(redis_client: Redis) -> None:
    """The revocation list is a real Redis key with a bounded lifetime."""
    from app.infrastructure.cache.revocation import RedisTokenRevocationStore  # noqa: PLC0415

    store = RedisTokenRevocationStore(redis_client)

    assert await store.is_revoked("token-1") is False
    await store.revoke("token-1", ttl_seconds=5)
    assert await store.is_revoked("token-1") is True

    await store.revoke("token-2", ttl_seconds=0.2)
    await asyncio.sleep(0.35)
    assert await store.is_revoked("token-2") is False
