"""Daily Content: the watchdog of the worker.

Runs in the Daily bot's process — another container than the worker — and
looks at the worker's heartbeat every ``DAILY_WATCHDOG_INTERVAL_SECONDS``.
When the worker has been silent for longer than
``DAILY_WATCHDOG_STALE_SECONDS`` the owner is told — through the Daily bot
itself, not through the outbox: the outbox is the worker's to send. While the
worker stays down the owner is reminded at most once per
``DAILY_WATCHDOG_COOLDOWN_MINUTES``; when it is back, the owner is told once.

* **One verdict at a time**: a Redis lock makes two watching processes take
  turns; the one that finds it taken skips the round.
* **Told, then remembered**: the new state is saved only after the owner was
  told — a message no owner received is tried again at the next round.
* **Nothing while Daily Content is off**: no reads, no messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from app.core.exceptions import DailySendError, LockBusyError
from app.core.logging import get_logger
from app.daily_bot import texts
from app.domain.daily_watchdog import WORKER_HEARTBEAT, WatchdogPolicy, judge

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.daily import DailyTextSender
    from app.domain.daily_watchdog import HeartbeatStore, WatchdogDecision, WatchdogStateStore
    from app.domain.locks import LockManager

logger = get_logger(__name__)

WATCHDOG_LOCK_KEY: Final = "daily-watchdog"
_LOCK_TTL_SECONDS: Final = 60.0
"""Outlives a check: reading two keys and telling a couple of owners."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class DailyWatchdog:
    """Watches the worker's heartbeat and tells the owner when it stops."""

    heartbeats: HeartbeatStore
    states: WatchdogStateStore
    locks: LockManager
    sender: DailyTextSender
    """The Daily bot: it reaches the owners who add content through it."""

    settings: DailySettings
    clock: Callable[[], datetime] = field(default=_utc_now)
    watching_since: datetime | None = None
    """When this process started watching: the start of a never-seen silence."""

    async def check(self) -> WatchdogDecision | None:
        """One round; ``None`` when nothing was looked at."""
        if not self.settings.enabled:
            return None
        try:
            async with self.locks.lock(WATCHDOG_LOCK_KEY, ttl_seconds=_LOCK_TTL_SECONDS):
                return await self._check()
        except LockBusyError:
            logger.info("daily_watchdog_round_skipped")
            return None

    async def _check(self) -> WatchdogDecision:
        now = self.clock()
        if self.watching_since is None:
            self.watching_since = now
        state = await self.states.load()
        decision = judge(
            state,
            last_beat=await self.heartbeats.last_beat(WORKER_HEARTBEAT),
            now=now,
            watching_since=self.watching_since,
            policy=WatchdogPolicy(
                stale_after=self.settings.watchdog_stale,
                cooldown=self.settings.watchdog_cooldown,
            ),
        )
        if decision.notice is not None and not await self._tell(decision):
            # Nobody heard it: the same verdict comes back next round.
            return decision
        if decision.state != state:
            await self.states.save(decision.state)
        return decision

    async def _tell(self, decision: WatchdogDecision) -> bool:
        """Tell every owner; whether at least one of them got it."""
        notice = decision.notice
        if notice is None:  # pragma: no cover — only called with a notice
            return True
        logger.warning(
            "daily_watchdog_notice",
            notice=notice.value,
            silence_seconds=int(decision.silence.total_seconds()),
        )
        owners = self.settings.admin_telegram_ids
        if not owners:
            logger.warning("daily_watchdog_no_owner", notice=notice.value)
            return True
        text = texts.watchdog_notice(notice, silence=decision.silence, downtime=decision.downtime)
        told = False
        for owner_id in owners:
            try:
                await self.sender.send_text(owner_id, text)
            except DailySendError as error:
                logger.warning("daily_watchdog_not_told", owner_id=owner_id, error=str(error))
            else:
                told = True
        return told


__all__ = ["WATCHDOG_LOCK_KEY", "DailyWatchdog"]
