"""Redis backed token revocation list.

Revoked ids are stored only until the token would have expired anyway, so the
key space cannot grow without bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from redis.asyncio import Redis

REVOCATION_KEY_PREFIX: Final = "revoked-token:"


class RedisTokenRevocationStore:
    """Remembers revoked token ids for the remainder of their lifetime."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def revoke(self, token_id: str, *, ttl_seconds: float) -> None:
        await self._client.set(
            f"{REVOCATION_KEY_PREFIX}{token_id}",
            "1",
            px=max(1, int(ttl_seconds * 1000)),
        )

    async def is_revoked(self, token_id: str) -> bool:
        return bool(await self._client.exists(f"{REVOCATION_KEY_PREFIX}{token_id}"))
