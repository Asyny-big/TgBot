"""Redis backed heartbeats and the Daily watchdog's memory.

Both live in Redis because they are shared between processes — the worker
writes its heartbeat, the Daily bot's process reads it — and because Redis
persistence is on in production, so a restart forgets neither.

A heartbeat expires after a week: a worker that was removed for good leaves no
key behind forever, and a week of silence is reported as silence anyway.
A value that cannot be read is treated as absent, never as a crash.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger
from app.domain.daily_watchdog import WatchdogState, WatchdogStatus

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)

HEARTBEAT_KEY_PREFIX: Final = "daily:heartbeat:"
WATCHDOG_STATE_KEY: Final = "daily:watchdog:state"
HEARTBEAT_TTL_SECONDS: Final = 7 * 24 * 3600


def _text(raw: object) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _moment(value: object) -> datetime | None:
    """An aware moment from its ISO form; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


class RedisHeartbeatStore:
    """Heartbeats as ISO moments under ``daily:heartbeat:<name>``."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def beat(self, name: str, *, at: datetime) -> None:
        await self._client.set(
            f"{HEARTBEAT_KEY_PREFIX}{name}",
            at.isoformat(),
            ex=HEARTBEAT_TTL_SECONDS,
        )

    async def last_beat(self, name: str) -> datetime | None:
        raw = _text(await self._client.get(f"{HEARTBEAT_KEY_PREFIX}{name}"))
        if raw is None:
            return None
        moment = _moment(raw)
        if moment is None:
            logger.warning("daily_heartbeat_unreadable", name=name)
        return moment


class RedisWatchdogStateStore:
    """The watchdog's state as one JSON document."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def load(self) -> WatchdogState:
        raw = _text(await self._client.get(WATCHDOG_STATE_KEY))
        if raw is None:
            return WatchdogState()
        try:
            document = json.loads(raw)
            return WatchdogState(
                status=WatchdogStatus(document["status"]),
                down_since=_moment(document.get("down_since")),
                notified_at=_moment(document.get("notified_at")),
            )
        except (TypeError, ValueError, KeyError):
            logger.warning("daily_watchdog_state_unreadable")
            return WatchdogState()

    async def save(self, state: WatchdogState) -> None:
        document = {
            "status": state.status.value,
            "down_since": state.down_since.isoformat() if state.down_since else None,
            "notified_at": state.notified_at.isoformat() if state.notified_at else None,
        }
        await self._client.set(WATCHDOG_STATE_KEY, json.dumps(document))


__all__ = [
    "HEARTBEAT_KEY_PREFIX",
    "HEARTBEAT_TTL_SECONDS",
    "WATCHDOG_STATE_KEY",
    "RedisHeartbeatStore",
    "RedisWatchdogStateStore",
]
