"""Daily Content: the watchdog of the worker — pure rules and its ports.

The worker writes a heartbeat while every one of its loops turns. Another
process — the Daily bot's — reads it: the worker cannot report its own death.

* silent for longer than ``stale_after``, the worker is **down**: the owner is
  told once;
* while it stays down, the owner is reminded at most once per ``cooldown``;
* when the heartbeat is fresh again, the owner is told it is back — once.

A heartbeat never seen at all counts from the moment the watchdog started
watching, so a stack that has just been deployed is given its time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from datetime import datetime

WORKER_HEARTBEAT: Final = "daily-worker"
"""The heartbeat the worker writes and the watchdog reads."""

_ZERO: Final = timedelta(0)


class WatchdogStatus(StrEnum):
    """What the watchdog last concluded about the worker."""

    OK = "ok"
    DOWN = "down"


class WatchdogNotice(StrEnum):
    """What the owner is to be told."""

    DOWN = "down"
    STILL_DOWN = "still_down"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchdogState:
    """The watchdog's memory between two checks — kept outside any process."""

    status: WatchdogStatus = WatchdogStatus.OK
    down_since: datetime | None = None
    """When the worker went silent: its last heartbeat, or the watch's start."""

    notified_at: datetime | None = None
    """When the owner was last told the worker is down."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchdogPolicy:
    """When silence means down, and how often the owner hears it again."""

    stale_after: timedelta
    cooldown: timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchdogDecision:
    """One check's conclusion: the state to keep, and what to tell the owner."""

    state: WatchdogState
    notice: WatchdogNotice | None
    silence: timedelta
    """How long the worker has been silent at this check."""

    downtime: timedelta | None = None
    """For ``RECOVERED``: how long the worker was down."""


def judge(
    state: WatchdogState,
    *,
    last_beat: datetime | None,
    now: datetime,
    watching_since: datetime,
    policy: WatchdogPolicy,
) -> WatchdogDecision:
    """The watchdog's verdict on one check."""
    silent_since = last_beat if last_beat is not None else watching_since
    silence = max(now - silent_since, _ZERO)
    down = silence > policy.stale_after
    if down and state.status is WatchdogStatus.OK:
        return WatchdogDecision(
            state=WatchdogState(
                status=WatchdogStatus.DOWN,
                down_since=silent_since,
                notified_at=now,
            ),
            notice=WatchdogNotice.DOWN,
            silence=silence,
        )
    if down:
        reminder_due = state.notified_at is None or now - state.notified_at >= policy.cooldown
        if not reminder_due:
            return WatchdogDecision(state=state, notice=None, silence=silence)
        return WatchdogDecision(
            state=replace(state, notified_at=now),
            notice=WatchdogNotice.STILL_DOWN,
            silence=silence,
        )
    if state.status is WatchdogStatus.DOWN:
        back_at = last_beat if last_beat is not None else now
        went_down = state.down_since if state.down_since is not None else back_at
        return WatchdogDecision(
            state=WatchdogState(),
            notice=WatchdogNotice.RECOVERED,
            silence=silence,
            downtime=max(back_at - went_down, _ZERO),
        )
    return WatchdogDecision(state=state, notice=None, silence=silence)


class HeartbeatStore(Protocol):
    """Port: where processes leave their heartbeat and others read it."""

    async def beat(self, name: str, *, at: datetime) -> None:
        """Record that ``name`` is alive at ``at``."""
        ...

    async def last_beat(self, name: str) -> datetime | None:
        """When ``name`` last beat; ``None`` if never (or long forgotten)."""
        ...


class WatchdogStateStore(Protocol):
    """Port: the watchdog's memory, shared by every process that watches."""

    async def load(self) -> WatchdogState:
        """The state the last check left; the default ``OK`` when there is none."""
        ...

    async def save(self, state: WatchdogState) -> None:
        """Keep the state for the next check."""
        ...


__all__ = [
    "WORKER_HEARTBEAT",
    "HeartbeatStore",
    "WatchdogDecision",
    "WatchdogNotice",
    "WatchdogPolicy",
    "WatchdogState",
    "WatchdogStateStore",
    "WatchdogStatus",
    "judge",
]
