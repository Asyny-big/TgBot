"""Process level infrastructure resources.

Both entrypoints (the FastAPI admin API and the aiogram bot) share this bundle,
so connection pools are created and released in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.infrastructure.cache.redis import RedisClient
from app.infrastructure.db.engine import Database

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Resources:
    """Infrastructure clients owned by the running process."""

    settings: Settings
    database: Database
    cache: RedisClient

    @classmethod
    def create(cls, settings: Settings) -> Resources:
        """Build the connection pools. No network I/O happens yet."""
        return cls(
            settings=settings,
            database=Database(settings.postgres),
            cache=RedisClient(settings.redis),
        )

    async def check(self) -> dict[str, bool]:
        """Probe every dependency and return its availability."""
        return {
            "database": await self.database.ping(),
            "redis": await self.cache.ping(),
        }

    async def close(self) -> None:
        """Release every pooled connection."""
        await self.database.dispose()
        await self.cache.close()
        logger.info("resources_released")
