"""The watchdog of the Daily worker.

The worker writes a heartbeat while its loops turn; the Daily bot's process
reads it. Silent too long, the worker is down: the owner hears it once, is
reminded no more often than the cooldown, and hears once that it is back.
Two watching processes never tell the same thing twice, and nothing happens
while Daily Content is off.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from app.core.config import DailySettings
from app.core.exceptions import DailySendBlockedError, DailySendError
from app.daily_bot import texts
from app.daily_bot.watchdog import run_watchdog
from app.daily_worker.health import Heartbeats
from app.daily_worker.loops import PulseLoop
from app.domain.daily_watchdog import (
    WORKER_HEARTBEAT,
    WatchdogNotice,
    WatchdogPolicy,
    WatchdogState,
    WatchdogStatus,
    judge,
)
from app.infrastructure.cache.daily_watchdog import (
    HEARTBEAT_KEY_PREFIX,
    HEARTBEAT_TTL_SECONDS,
    WATCHDOG_STATE_KEY,
    RedisHeartbeatStore,
    RedisWatchdogStateStore,
)
from app.services.daily_watchdog import WATCHDOG_LOCK_KEY, DailyWatchdog
from tests.concurrency import all_at_once
from tests.fakes import FakeHeartbeatStore, FakeLockManager, FakeWatchdogStateStore

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.infrastructure.cache.locks import RedisLockManager

OWNER = 9800
SECOND_OWNER = 9801
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
STALE = timedelta(minutes=5)
COOLDOWN = timedelta(minutes=60)
POLICY = WatchdogPolicy(stale_after=STALE, cooldown=COOLDOWN)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class RecordingSender:
    sent: list[tuple[int, str]] = field(default_factory=list)
    refuse: set[int] = field(default_factory=set)

    async def send_text(self, chat_id: int, text: str) -> int:
        if chat_id in self.refuse:
            message = "Forbidden: bot was blocked by the user"
            raise DailySendBlockedError(message)
        self.sent.append((chat_id, text))
        return len(self.sent)


def _settings(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "admin_telegram_ids": (OWNER,),
        "heartbeat_interval_seconds": 15,
        "watchdog_stale_seconds": int(STALE.total_seconds()),
        "watchdog_cooldown_minutes": int(COOLDOWN.total_seconds() // 60),
    }
    values.update(overrides)
    return DailySettings(**values)


# --------------------------------------------------------------------------- #
# The rules                                                                   #
# --------------------------------------------------------------------------- #


def _judge(state: WatchdogState, *, beat: datetime | None, now: datetime) -> Any:
    return judge(state, last_beat=beat, now=now, watching_since=T0, policy=POLICY)


def test_a_fresh_heartbeat_is_left_alone() -> None:
    decision = _judge(WatchdogState(), beat=T0, now=T0 + STALE)

    assert decision.notice is None
    assert decision.state == WatchdogState()
    assert decision.silence == STALE  # exactly the limit: not yet down


def test_silence_beyond_the_limit_is_reported_once_then_reminded_after_the_cooldown() -> None:
    went_silent = T0
    first = _judge(WatchdogState(), beat=went_silent, now=T0 + STALE + timedelta(seconds=1))
    soon = _judge(first.state, beat=went_silent, now=T0 + STALE + timedelta(minutes=30))
    later = _judge(soon.state, beat=went_silent, now=first.state.notified_at + COOLDOWN)
    after = _judge(later.state, beat=went_silent, now=later.state.notified_at + COOLDOWN / 2)

    assert first.notice is WatchdogNotice.DOWN
    assert first.state.status is WatchdogStatus.DOWN
    assert first.state.down_since == went_silent
    assert soon.notice is None
    assert soon.state == first.state
    assert later.notice is WatchdogNotice.STILL_DOWN
    assert later.state.notified_at == first.state.notified_at + COOLDOWN
    assert after.notice is None


def test_a_heartbeat_after_silence_is_reported_back_once_with_the_downtime() -> None:
    down = _judge(WatchdogState(), beat=T0, now=T0 + timedelta(minutes=10))
    back_at = T0 + timedelta(minutes=42)

    back = _judge(down.state, beat=back_at, now=back_at + timedelta(seconds=10))
    again = _judge(back.state, beat=back_at, now=back_at + timedelta(seconds=70))

    assert back.notice is WatchdogNotice.RECOVERED
    assert back.state == WatchdogState()
    assert back.downtime == timedelta(minutes=42)
    assert again.notice is None


def test_a_heartbeat_never_seen_counts_from_the_start_of_the_watch() -> None:
    early = _judge(WatchdogState(), beat=None, now=T0 + STALE)
    late = _judge(WatchdogState(), beat=None, now=T0 + STALE + timedelta(seconds=1))

    assert early.notice is None  # a stack just deployed gets its time
    assert late.notice is WatchdogNotice.DOWN
    assert late.state.down_since == T0


@pytest.mark.parametrize(
    ("notice", "silence", "downtime", "expected"),
    [
        (WatchdogNotice.DOWN, timedelta(minutes=6), None, "🚨 Daily-воркер молчит 6 мин"),
        (WatchdogNotice.STILL_DOWN, timedelta(minutes=125), None, "всё ещё молчит: 2 ч 5 мин"),
        (WatchdogNotice.RECOVERED, timedelta(0), timedelta(seconds=20), "Перерыв: 1 мин"),
    ],
)
def test_the_owner_reads_how_long(
    notice: WatchdogNotice,
    silence: timedelta,
    downtime: timedelta | None,
    expected: str,
) -> None:
    assert expected in texts.watchdog_notice(notice, silence=silence, downtime=downtime)


def test_the_watchdog_waits_for_three_heartbeats_at_least() -> None:
    three = DailySettings(heartbeat_interval_seconds=60, watchdog_stale_seconds=180)
    assert three.watchdog_stale == timedelta(minutes=3)
    with pytest.raises(ValueError, match="three heartbeat intervals"):
        DailySettings(heartbeat_interval_seconds=60, watchdog_stale_seconds=179)


# --------------------------------------------------------------------------- #
# The service                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class Watch:
    watchdog: DailyWatchdog
    beats: FakeHeartbeatStore
    states: FakeWatchdogStateStore
    sender: RecordingSender
    clock: Clock
    locks: FakeLockManager


def _status(watch: Watch) -> WatchdogStatus:
    return watch.states.state.status


def _watch(**settings: Any) -> Watch:
    clock, beats, states = Clock(), FakeHeartbeatStore(), FakeWatchdogStateStore()
    sender, locks = RecordingSender(), FakeLockManager()
    watchdog = DailyWatchdog(
        heartbeats=beats,
        states=states,
        locks=locks,
        sender=sender,
        settings=_settings(**settings),
        clock=clock,
    )
    return Watch(watchdog, beats, states, sender, clock, locks)


async def test_a_silent_worker_is_reported_to_every_owner_once() -> None:
    watch = _watch(admin_telegram_ids=(OWNER, SECOND_OWNER))
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    first = await watch.watchdog.check()
    watch.clock.now += timedelta(minutes=1)
    second = await watch.watchdog.check()

    assert first is not None
    assert first.notice is WatchdogNotice.DOWN
    assert second is not None
    assert second.notice is None
    assert [chat for chat, _ in watch.sender.sent] == [OWNER, SECOND_OWNER]
    assert all(text.startswith("🚨 Daily-воркер молчит 6 мин") for _, text in watch.sender.sent)
    assert watch.states.state.status is WatchdogStatus.DOWN
    assert watch.states.saves == 1
    assert [key for key, _ in watch.locks.acquired] == [WATCHDOG_LOCK_KEY] * 2


async def test_the_owner_is_reminded_after_the_cooldown_and_told_when_it_is_back() -> None:
    watch = _watch()
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)
    await watch.watchdog.check()

    watch.clock.now += COOLDOWN
    await watch.watchdog.check()
    watch.beats.beats[WORKER_HEARTBEAT] = watch.clock.now
    watch.clock.now += timedelta(seconds=30)
    await watch.watchdog.check()
    watch.clock.now += timedelta(minutes=1)
    watch.beats.beats[WORKER_HEARTBEAT] = watch.clock.now
    await watch.watchdog.check()

    sent = [text for _, text in watch.sender.sent]
    assert len(sent) == 3
    assert sent[0].startswith("🚨 Daily-воркер молчит")
    assert sent[1].startswith("🚨 Daily-воркер всё ещё молчит: 1 ч 6 мин")
    assert sent[2] == "✅ Daily-воркер снова работает. Перерыв: 1 ч 6 мин."
    assert watch.states.state == WatchdogState()


async def test_a_notice_nobody_received_is_tried_again() -> None:
    watch = _watch(admin_telegram_ids=(OWNER,))
    watch.sender.refuse = {OWNER}
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    unheard = await watch.watchdog.check()
    assert unheard is not None
    assert unheard.notice is WatchdogNotice.DOWN
    assert _status(watch) is WatchdogStatus.OK  # nothing remembered

    watch.sender.refuse = set()
    watch.clock.now += timedelta(minutes=1)
    heard = await watch.watchdog.check()

    assert heard is not None
    assert heard.notice is WatchdogNotice.DOWN
    assert len(watch.sender.sent) == 1
    assert _status(watch) is WatchdogStatus.DOWN


async def test_one_owner_hearing_it_is_enough() -> None:
    watch = _watch(admin_telegram_ids=(OWNER, SECOND_OWNER))
    watch.sender.refuse = {OWNER}
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    await watch.watchdog.check()
    watch.clock.now += timedelta(minutes=1)
    await watch.watchdog.check()

    assert [chat for chat, _ in watch.sender.sent] == [SECOND_OWNER]
    assert watch.states.state.status is WatchdogStatus.DOWN


async def test_with_no_owner_the_verdict_is_still_kept() -> None:
    watch = _watch(admin_telegram_ids=())
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    decision = await watch.watchdog.check()

    assert decision is not None
    assert decision.notice is WatchdogNotice.DOWN
    assert watch.sender.sent == []
    assert watch.states.state.status is WatchdogStatus.DOWN


async def test_while_daily_content_is_off_the_watchdog_does_nothing() -> None:
    watch = _watch(enabled=False)
    watch.clock.now = T0 + timedelta(days=1)

    assert await watch.watchdog.check() is None
    assert watch.sender.sent == []
    assert watch.states.saves == 0
    assert watch.locks.acquired == []


async def test_a_round_another_watcher_holds_is_skipped() -> None:
    watch = _watch()
    watch.locks.busy_keys.add(WATCHDOG_LOCK_KEY)
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    assert await watch.watchdog.check() is None
    assert watch.sender.sent == []
    assert watch.states.saves == 0


async def test_a_worker_never_seen_is_given_the_limit_from_the_first_check() -> None:
    watch = _watch()
    watch.clock.now = T0
    first = await watch.watchdog.check()
    watch.clock.now = T0 + STALE + timedelta(seconds=1)
    later = await watch.watchdog.check()

    assert first is not None
    assert first.notice is None
    assert later is not None
    assert later.notice is WatchdogNotice.DOWN


# --------------------------------------------------------------------------- #
# The loop in the Daily bot's process                                         #
# --------------------------------------------------------------------------- #


@dataclass
class FlakyWatchdog:
    checks: int = 0
    stop: asyncio.Event | None = None

    async def check(self) -> None:
        self.checks += 1
        if self.checks == 1:
            message = "Redis went away"
            raise ConnectionError(message)
        if self.checks == 3 and self.stop is not None:
            self.stop.set()


async def test_the_loop_survives_a_failed_check_and_stops_when_asked() -> None:
    stop = asyncio.Event()
    watchdog = FlakyWatchdog(stop=stop)

    await asyncio.wait_for(run_watchdog(watchdog, stop, interval=0.01), timeout=2)  # type: ignore[arg-type]

    assert watchdog.checks == 3


# --------------------------------------------------------------------------- #
# The worker's pulse                                                          #
# --------------------------------------------------------------------------- #


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_the_pulse_beats_while_every_loop_turns() -> None:
    ticks, store, clock = Ticks(), FakeHeartbeatStore(), Clock()
    heartbeats = Heartbeats(clock=ticks)
    pulse = PulseLoop(store=store, settings=_settings(), heartbeats=heartbeats, clock=clock)
    heartbeats.expect("sender", within=60)

    assert await pulse.run_once() is True
    assert store.beats == {WORKER_HEARTBEAT: T0}

    ticks.now = 61  # the sender stopped turning
    clock.now = T0 + timedelta(seconds=61)
    assert await pulse.run_once() is False
    assert store.beats == {WORKER_HEARTBEAT: T0}  # silence: the watchdog will notice

    heartbeats.beat("sender")
    heartbeats.beat("pulse")
    assert await pulse.run_once() is True
    assert store.beats == {WORKER_HEARTBEAT: T0 + timedelta(seconds=61)}


async def test_the_pulse_is_silent_while_daily_content_is_off() -> None:
    store = FakeHeartbeatStore()
    pulse = PulseLoop(
        store=store,
        settings=_settings(enabled=False),
        heartbeats=Heartbeats(clock=Ticks()),
        clock=Clock(),
    )

    assert await pulse.run_once() is False
    assert store.beats == {}


def test_the_pulse_beats_on_its_own_interval() -> None:
    pulse = PulseLoop(
        store=FakeHeartbeatStore(),
        settings=_settings(heartbeat_interval_seconds=20),
        heartbeats=Heartbeats(clock=Ticks()),
    )

    assert pulse.pause_after(True) == 20


# --------------------------------------------------------------------------- #
# Redis                                                                       #
# --------------------------------------------------------------------------- #


async def test_a_heartbeat_is_kept_in_redis_for_a_week(redis_client: Redis) -> None:
    store = RedisHeartbeatStore(redis_client)

    assert await store.last_beat(WORKER_HEARTBEAT) is None
    await store.beat(WORKER_HEARTBEAT, at=T0)

    assert await store.last_beat(WORKER_HEARTBEAT) == T0
    ttl = await redis_client.ttl(f"{HEARTBEAT_KEY_PREFIX}{WORKER_HEARTBEAT}")
    assert HEARTBEAT_TTL_SECONDS - 5 <= ttl <= HEARTBEAT_TTL_SECONDS


@pytest.mark.parametrize("garbage", ["yesterday", "2026-10-11T09:00:00"])
async def test_an_unreadable_heartbeat_counts_as_none(redis_client: Redis, garbage: str) -> None:
    await redis_client.set(f"{HEARTBEAT_KEY_PREFIX}{WORKER_HEARTBEAT}", garbage)

    assert await RedisHeartbeatStore(redis_client).last_beat(WORKER_HEARTBEAT) is None


async def test_the_watchdogs_memory_survives_in_redis(redis_client: Redis) -> None:
    store = RedisWatchdogStateStore(redis_client)
    state = WatchdogState(status=WatchdogStatus.DOWN, down_since=T0, notified_at=T0)

    assert await store.load() == WatchdogState()
    await store.save(state)

    assert await RedisWatchdogStateStore(redis_client).load() == state
    await redis_client.set(WATCHDOG_STATE_KEY, "{not json")
    assert await store.load() == WatchdogState()


async def test_two_watching_processes_tell_the_owner_once(
    redis_client: Redis,
    live_locks: RedisLockManager,
) -> None:
    """The Redis lock serialises them; the shared state keeps the second quiet."""
    beats = RedisHeartbeatStore(redis_client)
    await beats.beat(WORKER_HEARTBEAT, at=T0)
    sender = RecordingSender()
    clock = Clock(T0 + timedelta(minutes=6))

    def watcher() -> DailyWatchdog:
        return DailyWatchdog(
            heartbeats=RedisHeartbeatStore(redis_client),
            states=RedisWatchdogStateStore(redis_client),
            locks=live_locks,
            sender=sender,
            settings=_settings(),
            clock=clock,
        )

    results = await all_at_once(watcher().check(), watcher().check())
    again = await watcher().check()

    assert not [result for result in results if isinstance(result, BaseException)]
    assert len(sender.sent) == 1
    assert again is not None
    assert again.notice is None
    state = await RedisWatchdogStateStore(redis_client).load()
    assert state.status is WatchdogStatus.DOWN


async def test_the_pulse_and_the_watchdog_meet_in_redis(redis_client: Redis) -> None:
    """The worker's pulse, the bot's watchdog: down while silent, back once beating."""
    clock, ticks = Clock(), Ticks()
    store = RedisHeartbeatStore(redis_client)
    pulse = PulseLoop(
        store=store,
        settings=_settings(),
        heartbeats=Heartbeats(clock=ticks),
        clock=clock,
    )
    sender = RecordingSender()
    watchdog = DailyWatchdog(
        heartbeats=store,
        states=RedisWatchdogStateStore(redis_client),
        locks=FakeLockManager(),
        sender=sender,
        settings=_settings(),
        clock=clock,
    )

    await pulse.run_once()
    await watchdog.check()
    clock.now += timedelta(minutes=10)  # the worker died: no pulse since
    await watchdog.check()
    await pulse.run_once()  # restarted
    clock.now += timedelta(seconds=30)
    await watchdog.check()

    sent = [text for _, text in sender.sent]
    assert len(sent) == 2
    assert sent[0].startswith("🚨 Daily-воркер молчит 10 мин")
    assert sent[1] == "✅ Daily-воркер снова работает. Перерыв: 10 мин."


async def test_a_failing_telegram_never_breaks_the_check() -> None:
    watch = _watch()

    class Broken:
        async def send_text(self, chat_id: int, text: str) -> int:
            del chat_id, text
            message = "Telegram is unavailable"
            raise DailySendError(message)

    watch.watchdog.sender = Broken()
    watch.beats.beats[WORKER_HEARTBEAT] = T0
    watch.clock.now = T0 + timedelta(minutes=6)

    decision = await watch.watchdog.check()

    assert decision is not None
    assert decision.notice is WatchdogNotice.DOWN
    assert watch.states.saves == 0
