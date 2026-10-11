"""The Daily worker's loops and their supervisor, on stub services.

What is tested here is the turning, not the work: a failing pass does not
stop a loop, every pass beats the heartbeat, a stop request waits for the
pass in flight, Daily Content switched off means no service is even called,
and a loop that dies on its own takes the process down with it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.core.config import DailySettings
from app.daily_worker.health import Heartbeats
from app.daily_worker.loops import (
    EvaluatorLoop,
    ExpiryLoop,
    OutboxLoop,
    PlannerLoop,
    SenderLoop,
    SendPass,
    WorkerLoop,
)
from app.daily_worker.runner import DailyWorker
from app.domain.daily import DeliveryCounts, DeliveryStep, StockSummary
from app.domain.daily_alerts import AlertObservation, TokenState
from app.domain.enums import DailyAlertKey
from app.services.daily_delivery import DeliveryReport
from app.services.daily_outbox import OutboxReport, OutboxStep
from app.services.daily_planning import PlanReport
from app.services.daily_stock import StockSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable


def _settings(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "send_batch_size": 3,
        "sender_idle_seconds": 5,
        "lease_seconds": 300,
        "low_stock_days": 3,
    }
    values.update(overrides)
    return DailySettings(**values)


@dataclass
class Ticks:
    """A monotonic clock moved by hand."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class StubPacer:
    paused: float = 0.0

    def paused_for(self) -> float:
        return self.paused


# --------------------------------------------------------------------------- #
# Turning                                                                     #
# --------------------------------------------------------------------------- #


class ScriptedLoop(WorkerLoop):
    """Runs the given passes in order, then asks the worker to stop."""

    name = "scripted"

    def __init__(
        self,
        passes: list[Callable[[], Any]],
        stop: asyncio.Event,
        heartbeats: Heartbeats,
    ) -> None:
        self._passes = passes
        self._stop = stop
        self.done: list[Any] = []
        super().__init__(heartbeats=heartbeats, interval=0)

    async def run_once(self) -> object:
        step = self._passes.pop(0)
        if not self._passes:
            self._stop.set()
        result = await step()
        self.done.append(result)
        return result


async def test_a_failing_pass_is_logged_and_the_loop_goes_on() -> None:
    ticks, stop = Ticks(), asyncio.Event()
    heartbeats = Heartbeats(clock=ticks)

    async def broken() -> None:
        ticks.now += 10
        message = "PostgreSQL is down"
        raise ConnectionError(message)

    async def fine() -> str:
        ticks.now += 10
        return "ok"

    loop = ScriptedLoop([broken, broken, fine], stop, heartbeats)
    await asyncio.wait_for(loop.run_forever(stop), timeout=5)

    assert loop.done == ["ok"]
    # It beat after the failed passes too: the last beat is from the last pass.
    assert heartbeats.ages() == {"scripted": 0}


async def test_a_failed_pass_still_counts_as_a_heartbeat() -> None:
    """A loop failing because PostgreSQL is down is alive, not stuck."""
    ticks, stop = Ticks(), asyncio.Event()
    heartbeats = Heartbeats(clock=ticks)

    async def broken() -> None:
        ticks.now += 30
        message = "PostgreSQL is down"
        raise ConnectionError(message)

    loop = ScriptedLoop([broken], stop, heartbeats)
    await asyncio.wait_for(loop.run_forever(stop), timeout=5)

    assert heartbeats.ages() == {"scripted": 0}
    assert heartbeats.stale() == ()


async def test_a_stop_request_waits_for_the_pass_in_flight() -> None:
    stop = asyncio.Event()
    started, release = asyncio.Event(), asyncio.Event()

    async def long_batch() -> str:
        started.set()
        await release.wait()
        return "recorded"

    loop = ScriptedLoop([long_batch, long_batch], stop, Heartbeats())
    running = asyncio.create_task(loop.run_forever(stop))
    await started.wait()
    stop.set()  # SIGTERM in the middle of a batch
    await asyncio.sleep(0)
    assert not running.done()
    release.set()
    await asyncio.wait_for(running, timeout=5)

    assert loop.done == ["recorded"]  # finished and recorded, then stopped


def test_no_loop_is_stale_sooner_than_two_minutes() -> None:
    heartbeats = Heartbeats()
    loop = ScriptedLoop([], asyncio.Event(), heartbeats)

    assert loop.max_silence() == 120


# --------------------------------------------------------------------------- #
# Sending                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class StubDelivery:
    reports: list[DeliveryReport] = field(default_factory=list)
    calls: int = 0
    expire_batches: list[int] = field(default_factory=list)

    async def process_due(self, *, limit: int = 20) -> tuple[DeliveryReport, ...]:
        self.calls += 1
        return tuple(self.reports[:limit])

    async def expire_overdue(self, *, limit: int = 100) -> int:
        del limit
        self.calls += 1
        return self.expire_batches.pop(0) if self.expire_batches else 0


@dataclass
class StubNotices:
    told: list[int] = field(default_factory=list)

    async def exhausted(self, user_id: int) -> bool:
        self.told.append(user_id)
        return True


def _sender(
    delivery: StubDelivery,
    *,
    notices: StubNotices | None = None,
    pacer: StubPacer | None = None,
    **settings: Any,
) -> SenderLoop:
    return SenderLoop(
        delivery=delivery,  # type: ignore[arg-type]
        notices=notices or StubNotices(),  # type: ignore[arg-type]
        settings=_settings(**settings),
        pacer=pacer or StubPacer(),  # type: ignore[arg-type]
        heartbeats=Heartbeats(),
    )


def _report(step: DeliveryStep, user_id: int = 1) -> DeliveryReport:
    return DeliveryReport(delivery_id=user_id, step=step, user_id=user_id)


async def test_the_sender_tells_each_buyer_who_ran_out() -> None:
    delivery = StubDelivery(
        reports=[
            _report(DeliveryStep.SENT, 1),
            _report(DeliveryStep.NO_CONTENT, 2),
            _report(DeliveryStep.NO_CONTENT, 3),
        ]
    )
    notices = StubNotices()

    result = await _sender(delivery, notices=notices).run_once()

    assert result.claimed == 3
    assert result.steps == {"sent": 1, "no_content": 2}
    assert notices.told == [2, 3]


async def test_the_sender_claims_nothing_while_held_by_flood_control() -> None:
    delivery = StubDelivery(reports=[_report(DeliveryStep.SENT)])
    loop = _sender(delivery, pacer=StubPacer(paused=40))

    result = await loop.run_once()

    assert result == SendPass(paused=True)
    assert delivery.calls == 0  # no attempt spent on sends that cannot go
    assert loop.pause_after(result) == 40


def test_the_sender_paces_itself() -> None:
    loop = _sender(StubDelivery())

    assert loop.pause_after(SendPass(claimed=3)) == 0  # a full batch: more is waiting
    assert loop.pause_after(SendPass(claimed=1)) == 5  # idle
    long_pause = _sender(StubDelivery(), pacer=StubPacer(paused=10_000))
    assert long_pause.pause_after(SendPass(paused=True)) == long_pause.max_silence() / 2
    assert loop.max_silence() == 420  # the lease bounds a pass


async def test_the_outbox_loop_sends_in_batches_and_respects_the_pause() -> None:
    @dataclass
    class StubOutbox:
        calls: list[int] = field(default_factory=list)

        async def process_due(self, *, limit: int = 10) -> tuple[OutboxReport, ...]:
            self.calls.append(limit)
            return (OutboxReport(message_id=1, step=OutboxStep.SENT),)

    outbox, pacer = StubOutbox(), StubPacer()
    loop = OutboxLoop(
        outbox=outbox,  # type: ignore[arg-type]
        settings=_settings(),
        pacer=pacer,  # type: ignore[arg-type]
        heartbeats=Heartbeats(),
    )

    sent = await loop.run_once()
    pacer.paused = 5
    held = await loop.run_once()

    assert sent.steps == {"sent": 1}
    assert held.paused
    assert outbox.calls == [3]


# --------------------------------------------------------------------------- #
# Expiry, planning, evaluation                                                #
# --------------------------------------------------------------------------- #


async def test_expiry_drains_every_batch() -> None:
    @dataclass
    class StubOutbox:
        batches: list[int] = field(default_factory=lambda: [4])

        async def expire_overdue(self, *, limit: int = 100) -> int:
            del limit
            return self.batches.pop(0) if self.batches else 0

    delivery = StubDelivery(expire_batches=[100, 100, 7])
    loop = ExpiryLoop(
        delivery=delivery,  # type: ignore[arg-type]
        outbox=StubOutbox(),  # type: ignore[arg-type]
        settings=_settings(),
        heartbeats=Heartbeats(),
    )

    assert await loop.run_once() == 211
    assert delivery.calls == 3


async def test_the_planner_loop_plans() -> None:
    @dataclass
    class StubPlanning:
        calls: int = 0

        async def plan(self) -> PlanReport:
            self.calls += 1
            return PlanReport(scanned=4, opened=2)

    planning = StubPlanning()
    loop = PlannerLoop(planning=planning, settings=_settings(), heartbeats=Heartbeats())  # type: ignore[arg-type]

    assert await loop.run_once() == PlanReport(scanned=4, opened=2)
    assert planning.calls == 1


@dataclass
class StubStock:
    broken: bool = False

    async def snapshot(self) -> StockSnapshot:
        if self.broken:
            message = "the stock query failed"
            raise RuntimeError(message)
        return StockSnapshot(
            summary=StockSummary(
                active_users=10,
                average_remaining=2,
                minimum_remaining=0,
                median_remaining=2,
                exhausted_users=1,
                users_at_risk=10,
                forecast_days=2,
            ),
            active_items=5,
            add_rate_per_day=0.0,
        )

    async def recent_outcomes(self) -> DeliveryCounts:
        return DeliveryCounts(sent=10, failed=5)


@dataclass
class StubAlerts:
    observed: list[tuple[DailyAlertKey, AlertObservation]] = field(default_factory=list)

    async def observe(self, key: DailyAlertKey, observation: AlertObservation) -> None:
        self.observed.append((key, observation))


@dataclass
class StubProbe:
    state: TokenState = TokenState.UNAUTHORIZED

    async def check(self) -> TokenState:
        return self.state


def _evaluator(stock: StubStock, alerts: StubAlerts, **settings: Any) -> EvaluatorLoop:
    return EvaluatorLoop(
        stock=stock,  # type: ignore[arg-type]
        alerts=alerts,  # type: ignore[arg-type]
        probe=StubProbe(),
        settings=_settings(**settings),
        heartbeats=Heartbeats(),
    )


async def test_the_evaluator_observes_every_alert() -> None:
    alerts = StubAlerts()

    failures = await _evaluator(StubStock(), alerts).run_once()

    assert failures == 0
    by_key = dict(alerts.observed)
    assert list(by_key) == [
        DailyAlertKey.LOW_STOCK,
        DailyAlertKey.CONTENT_EXHAUSTED,
        DailyAlertKey.DELIVERY_FAILURES,
        DailyAlertKey.DAILY_TOKEN_UNAUTHORIZED,
    ]
    assert all(observation.firing for observation in by_key.values())


async def test_one_failing_check_never_stops_the_others() -> None:
    alerts = StubAlerts()

    failures = await _evaluator(StubStock(broken=True), alerts).run_once()

    assert failures == 1
    assert [key for key, _ in alerts.observed] == [
        DailyAlertKey.DELIVERY_FAILURES,
        DailyAlertKey.DAILY_TOKEN_UNAUTHORIZED,
    ]


async def test_while_daily_content_is_off_no_loop_calls_anything() -> None:
    delivery = StubDelivery(reports=[_report(DeliveryStep.SENT)])
    notices, alerts = StubNotices(), StubAlerts()

    @dataclass
    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(name)

    off = _settings(enabled=False)
    sender = SenderLoop(
        delivery=delivery,  # type: ignore[arg-type]
        notices=notices,  # type: ignore[arg-type]
        settings=off,
        pacer=StubPacer(),  # type: ignore[arg-type]
        heartbeats=Heartbeats(),
    )
    outbox = OutboxLoop(
        outbox=Untouchable(),  # type: ignore[arg-type]
        settings=off,
        pacer=StubPacer(),  # type: ignore[arg-type]
        heartbeats=Heartbeats(),
    )
    expiry = ExpiryLoop(
        delivery=Untouchable(),  # type: ignore[arg-type]
        outbox=Untouchable(),  # type: ignore[arg-type]
        settings=off,
        heartbeats=Heartbeats(),
    )

    assert await sender.run_once() == SendPass()
    assert await outbox.run_once() == SendPass()
    assert await expiry.run_once() == 0
    assert await _evaluator(StubStock(), alerts, enabled=False).run_once() == 0
    assert delivery.calls == 0
    assert alerts.observed == []


# --------------------------------------------------------------------------- #
# The supervisor                                                              #
# --------------------------------------------------------------------------- #


class TurningLoop(WorkerLoop):
    """Turns until stopped."""

    name = "turning"

    def __init__(self, heartbeats: Heartbeats) -> None:
        self.passes = 0
        super().__init__(heartbeats=heartbeats, interval=0.01)

    async def run_once(self) -> object:
        self.passes += 1
        return None


class DyingLoop(TurningLoop):
    """Ends on its own — what a bug in a loop looks like."""

    name = "dying"

    async def run_forever(self, stop: asyncio.Event) -> None:
        del stop
        await asyncio.sleep(0.01)


class StubbornLoop(TurningLoop):
    """Never notices the stop request."""

    name = "stubborn"

    async def run_forever(self, stop: asyncio.Event) -> None:
        del stop
        await asyncio.sleep(3600)


async def test_the_worker_stops_every_loop_on_request() -> None:
    heartbeats = Heartbeats()
    loops = (TurningLoop(heartbeats), TurningLoop(heartbeats))
    stop = asyncio.Event()
    worker = DailyWorker(loops=loops, heartbeats=heartbeats)
    running = asyncio.create_task(worker.run(stop, grace_seconds=5))
    await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert all(loop.passes > 0 for loop in loops)


async def test_the_worker_lets_the_pass_in_flight_finish() -> None:
    heartbeats, stop = Heartbeats(), asyncio.Event()
    started, release = asyncio.Event(), asyncio.Event()

    async def long_batch() -> str:
        started.set()
        await release.wait()
        return "recorded"

    loop = ScriptedLoop([long_batch, long_batch], asyncio.Event(), heartbeats)
    running = asyncio.create_task(
        DailyWorker(loops=(loop,), heartbeats=heartbeats).run(stop, grace_seconds=5)
    )
    await started.wait()
    stop.set()
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(running, timeout=5)

    assert loop.done == ["recorded"]


async def test_a_loop_that_dies_takes_the_worker_down() -> None:
    heartbeats = Heartbeats()
    survivor = TurningLoop(heartbeats)
    worker = DailyWorker(loops=(survivor, DyingLoop(heartbeats)), heartbeats=heartbeats)

    with pytest.raises(SystemExit) as exited:
        await asyncio.wait_for(worker.run(asyncio.Event(), grace_seconds=5), timeout=5)

    assert exited.value.code == 1


async def test_a_loop_that_ignores_the_stop_is_cancelled_after_the_grace() -> None:
    heartbeats = Heartbeats()
    worker = DailyWorker(loops=(StubbornLoop(heartbeats),), heartbeats=heartbeats)
    stop = asyncio.Event()
    stop.set()
    started = datetime.now(UTC)

    await asyncio.wait_for(worker.run(stop, grace_seconds=0.05), timeout=5)

    assert (datetime.now(UTC) - started).total_seconds() < 2
