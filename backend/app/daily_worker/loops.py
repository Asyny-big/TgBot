"""The Daily worker's loops.

Every loop is one ``run_once`` — a pass that can be tested on its own — turned
by ``run_forever``:

* an iteration that fails is logged and the loop goes on: PostgreSQL or
  Telegram being down must never kill it;
* the loop beats its heartbeat after every iteration, failed or not;
* a stop request is honoured *between* iterations: a batch being sent is
  finished and recorded first, so no lease is left behind by a clean stop.

While Daily Content is off every loop turns and does nothing at all: nothing
is planned, sent, expired, evaluated or recorded.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final

from app.core.logging import get_logger
from app.domain.daily import DeliveryStep
from app.domain.daily_alerts import (
    exhausted_observation,
    failures_observation,
    low_stock_observation,
    token_observation,
)
from app.domain.enums import DailyAlertKey

if TYPE_CHECKING:
    from app.core.config import DailySettings
    from app.daily_worker.health import Heartbeats
    from app.domain.daily_alerts import TokenProbe
    from app.infrastructure.telegram.daily_pacing import SendPacer
    from app.services.daily_alerts import DailyAlertService
    from app.services.daily_delivery import DailyDeliveryService
    from app.services.daily_notices import DailyNoticeService
    from app.services.daily_outbox import DailyOutboxService
    from app.services.daily_planning import DailyPlanningService
    from app.services.daily_stock import DailyStockService

logger = get_logger(__name__)

MIN_SILENCE_SECONDS: Final = 120.0
"""No loop is reported stale sooner than this."""

EXPIRY_BATCH: Final = 100


class WorkerLoop:
    """One loop of the worker: ``run_once``, turned until asked to stop."""

    name: ClassVar[str] = "loop"

    def __init__(self, *, heartbeats: Heartbeats, interval: float) -> None:
        self._heartbeats = heartbeats
        self._interval = interval
        heartbeats.expect(self.name, within=self.max_silence())

    def max_silence(self) -> float:
        """How long the loop may go without finishing an iteration."""
        return max(3 * self._interval, MIN_SILENCE_SECONDS)

    async def run_once(self) -> object:
        """One pass. Subclasses do the work."""
        raise NotImplementedError

    def pause_after(self, result: object) -> float:
        """Seconds to wait before the next pass."""
        del result
        return self._interval

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Turn until ``stop`` is set; survive every failure of a pass."""
        logger.info("daily_worker_loop_started", loop=self.name, interval=self._interval)
        while not stop.is_set():
            result: object = None
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # the loop must survive anything
                logger.error(  # noqa: TRY400 — one failed pass must not stop the loop
                    "daily_worker_iteration_failed",
                    loop=self.name,
                    error=str(error),
                    error_type=type(error).__name__,
                )
            finally:
                self._heartbeats.beat(self.name)
            await _sleep_unless_stopped(stop, self.pause_after(result))
        logger.info("daily_worker_loop_stopped", loop=self.name)


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        await asyncio.sleep(0)
        return
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


# --------------------------------------------------------------------------- #
# Planning                                                                    #
# --------------------------------------------------------------------------- #


class PlannerLoop(WorkerLoop):
    """Opens each buyer's delivery once their window opens."""

    name = "planner"

    def __init__(
        self,
        *,
        planning: DailyPlanningService,
        settings: DailySettings,
        heartbeats: Heartbeats,
    ) -> None:
        self._planning = planning
        super().__init__(heartbeats=heartbeats, interval=settings.planner_interval_seconds)

    async def run_once(self) -> object:
        return await self._planning.plan()


# --------------------------------------------------------------------------- #
# Sending                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class SendPass:
    """What one pass of a sending loop did."""

    claimed: int = 0
    steps: Counter[str] = field(default_factory=Counter)
    paused: bool = False
    """Nothing was claimed: the worker is held by Telegram's flood control."""


class _SendingLoop(WorkerLoop):
    """A loop that claims a batch and sends it within the worker's budget."""

    def __init__(
        self,
        *,
        settings: DailySettings,
        pacer: SendPacer,
        heartbeats: Heartbeats,
    ) -> None:
        self._settings = settings
        self._pacer = pacer
        super().__init__(heartbeats=heartbeats, interval=settings.sender_idle_seconds)

    def max_silence(self) -> float:
        # One pass may send a whole batch, each send as slow as Telegram allows;
        # the lease bounds it: past it, the engine hands the rest back.
        return max(super().max_silence(), self._settings.lease_seconds + MIN_SILENCE_SECONDS)

    def pause_after(self, result: object) -> float:
        paused = self._pacer.paused_for()
        if paused > 0:
            # Wake when the pause is over — but beat in between.
            return min(paused, self.max_silence() / 2)
        if isinstance(result, SendPass) and result.claimed >= self._settings.send_batch_size:
            return 0  # a backlog: the next batch at once
        return self._interval

    def _held_by_flood_control(self) -> bool:
        return self._pacer.paused_for() > 0


class SenderLoop(_SendingLoop):
    """Sends the deliveries due now; tells exhausted buyers so, once."""

    name = "sender"

    def __init__(
        self,
        *,
        delivery: DailyDeliveryService,
        notices: DailyNoticeService,
        settings: DailySettings,
        pacer: SendPacer,
        heartbeats: Heartbeats,
    ) -> None:
        self._delivery = delivery
        self._notices = notices
        super().__init__(settings=settings, pacer=pacer, heartbeats=heartbeats)

    async def run_once(self) -> SendPass:
        if not self._settings.enabled:
            return SendPass()
        if self._held_by_flood_control():
            # Claiming now would only spend attempts on sends that cannot go.
            return SendPass(paused=True)
        reports = await self._delivery.process_due(limit=self._settings.send_batch_size)
        for report in reports:
            if report.step is DeliveryStep.NO_CONTENT and report.user_id is not None:
                await self._notices.exhausted(report.user_id)
        steps = Counter(report.step.value for report in reports)
        if reports:
            logger.info("daily_sender_pass", **steps)
        return SendPass(claimed=len(reports), steps=steps)


class OutboxLoop(_SendingLoop):
    """Sends the alerts and notices due now."""

    name = "outbox"

    def __init__(
        self,
        *,
        outbox: DailyOutboxService,
        settings: DailySettings,
        pacer: SendPacer,
        heartbeats: Heartbeats,
    ) -> None:
        self._outbox = outbox
        super().__init__(settings=settings, pacer=pacer, heartbeats=heartbeats)

    async def run_once(self) -> SendPass:
        if not self._settings.enabled:
            return SendPass()
        if self._held_by_flood_control():
            return SendPass(paused=True)
        reports = await self._outbox.process_due(limit=self._settings.send_batch_size)
        return SendPass(
            claimed=len(reports),
            steps=Counter(report.step.value for report in reports),
        )


# --------------------------------------------------------------------------- #
# Expiry                                                                      #
# --------------------------------------------------------------------------- #


class ExpiryLoop(WorkerLoop):
    """Closes deliveries and messages whose deadline passed; gives items back."""

    name = "expiry"

    def __init__(
        self,
        *,
        delivery: DailyDeliveryService,
        outbox: DailyOutboxService,
        settings: DailySettings,
        heartbeats: Heartbeats,
    ) -> None:
        self._delivery = delivery
        self._outbox = outbox
        self._settings = settings
        super().__init__(heartbeats=heartbeats, interval=settings.expire_interval_seconds)

    async def run_once(self) -> int:
        if not self._settings.enabled:
            return 0
        expired = 0
        while (batch := await self._delivery.expire_overdue(limit=EXPIRY_BATCH)) > 0:
            expired += batch
            if batch < EXPIRY_BATCH:
                break
        while (batch := await self._outbox.expire_overdue(limit=EXPIRY_BATCH)) > 0:
            expired += batch
            if batch < EXPIRY_BATCH:
                break
        return expired


# --------------------------------------------------------------------------- #
# Evaluation                                                                  #
# --------------------------------------------------------------------------- #


class EvaluatorLoop(WorkerLoop):
    """Evaluates the stock, the failures and the token; raises the owner's alerts.

    Each check fails on its own: a failing stock query never stops the token
    from being checked.
    """

    name = "evaluator"

    def __init__(
        self,
        *,
        stock: DailyStockService,
        alerts: DailyAlertService,
        probe: TokenProbe,
        settings: DailySettings,
        heartbeats: Heartbeats,
    ) -> None:
        self._stock = stock
        self._alerts = alerts
        self._probe = probe
        self._settings = settings
        super().__init__(heartbeats=heartbeats, interval=settings.evaluate_interval_seconds)

    async def run_once(self) -> int:
        """Return how many checks failed."""
        if not self._settings.enabled:
            return 0
        failures = 0
        for name, check in (
            ("stock", self._check_stock),
            ("failures", self._check_failures),
            ("token", self._check_token),
        ):
            try:
                await check()
            except Exception as error:  # one check must not stop the others
                failures += 1
                logger.error(  # noqa: TRY400 — the next evaluation tries again
                    "daily_evaluation_failed",
                    check=name,
                    error=str(error),
                )
        return failures

    async def _check_stock(self) -> None:
        snapshot = await self._stock.snapshot()
        await self._alerts.observe(
            DailyAlertKey.LOW_STOCK,
            low_stock_observation(
                snapshot.summary,
                active_items=snapshot.active_items,
                threshold_days=self._settings.low_stock_days,
            ),
        )
        await self._alerts.observe(
            DailyAlertKey.CONTENT_EXHAUSTED,
            exhausted_observation(snapshot.summary),
        )

    async def _check_failures(self) -> None:
        counts = await self._stock.recent_outcomes()
        await self._alerts.observe(
            DailyAlertKey.DELIVERY_FAILURES,
            failures_observation(sent=counts.sent, failed=counts.failed),
        )

    async def _check_token(self) -> None:
        await self._alerts.observe(
            DailyAlertKey.DAILY_TOKEN_UNAUTHORIZED,
            token_observation(await self._probe.check()),
        )


__all__ = [
    "EvaluatorLoop",
    "ExpiryLoop",
    "OutboxLoop",
    "PlannerLoop",
    "SendPass",
    "SenderLoop",
    "WorkerLoop",
]
