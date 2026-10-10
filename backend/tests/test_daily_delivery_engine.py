"""The Daily Content delivery engine, end to end (PostgreSQL), Telegram scripted.

The scenarios that matter most:

* a process dies after Telegram took the message but before it was recorded —
  the lease runs out, the next worker sends the *same* item again, and the
  history shows the crash and the possible duplicate;
* Telegram definitely refuses — the item stays available to the buyer;
* temporary failures are retried with the same item, flood control respected;
* one item a day, whatever runs twice; two workers never send one delivery.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from app.core.config import DailySettings
from app.core.exceptions import (
    DailyContentRejectedError,
    DailySendBlockedError,
    DailySendRejectedError,
    DailySendTransientError,
)
from app.domain.commands import DailyContentDraft, ProductDraft, PurchaseDraft, UserDraft
from app.domain.daily import DeliveryStep
from app.domain.enums import (
    Currency,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    PaymentProvider,
    PurchaseStatus,
)
from app.services.daily_delivery import DailyDeliveryService

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.domain.entities import DailyContent, DailyDelivery, DailySubscription
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9951
T0 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
NOON = 12 * 60
LEASE = timedelta(seconds=300)


class ProcessDied(BaseException):
    """The worker process dies: nothing after this line of the pass runs."""


ACCEPT = None
CRASH_AFTER_ACCEPT = "telegram took it, then the process died"


@dataclass
class ScriptedTelegram:
    """The ``DailyContentSender`` port, played by a script.

    Each send takes the next step: ``ACCEPT``, an exception to raise before
    Telegram took anything, or ``CRASH_AFTER_ACCEPT``.
    """

    script: list[object] = field(default_factory=list)
    calls: list[tuple[int, int]] = field(default_factory=list)
    """Every (buyer, item) the engine tried to send."""

    accepted: list[tuple[int, int]] = field(default_factory=list)
    """Every (buyer, item) Telegram actually took — what buyers saw."""

    protect: list[bool] = field(default_factory=list)
    hold: asyncio.Event | None = None
    """When set, a send waits for it — to keep an attempt in flight."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    """Set as soon as a send begins."""

    before_send: Callable[[], None] | None = None
    """Runs at the start of every send — to let time pass inside one."""

    _next_id: int = 5000

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        self.calls.append((chat_id, content.id))
        self.protect.append(protect_content)
        self.started.set()
        if self.before_send is not None:
            self.before_send()
        step = self.script.pop(0) if self.script else ACCEPT
        if self.hold is not None:
            await self.hold.wait()
        if isinstance(step, Exception):
            raise step
        self._next_id += 1
        self.accepted.append((chat_id, content.id))
        if step == CRASH_AFTER_ACCEPT:
            raise ProcessDied
        return self._next_id


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _engine(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    telegram: ScriptedTelegram,
    clock: Clock,
    **settings: Any,
) -> DailyDeliveryService:
    values: dict[str, Any] = {
        "enabled": True,
        "lease_seconds": int(LEASE.total_seconds()),
        "retry_initial_seconds": 30,
        "retry_max_seconds": 900,
        "max_attempts": 5,
        "no_content_recheck_seconds": 900,
        "recovery_hours": 12,
    }
    values.update(settings)
    return DailyDeliveryService(
        uow_factory=uow_factory,
        sender=telegram,
        settings=DailySettings(**values),
        clock=clock,
        jitter=lambda: 0.0,
    )


# --------------------------------------------------------------------------- #
# Seeding                                                                     #
# --------------------------------------------------------------------------- #


async def _subscriber(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    user_id: int = BUYER,
    *,
    completed: int = 15,
    activate: bool = True,
) -> None:
    async with uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for _ in range(completed):
            product = await uow.products.create(
                ProductDraft(
                    slug=f"e{uuid4().hex[:10]}",
                    title="Pack",
                    description="",
                    delivery_url="https://t.me/+pack",
                    price_stars=100,
                )
            )
            paid = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product.id,
                    provider=PaymentProvider.STARS,
                    amount=Decimal(100),
                    currency=Currency.XTR,
                    external_id=uuid4().hex,
                    status=PurchaseStatus.PAID,
                )
            )
            await uow.purchases.mark_delivered(paid.id, delivered_url="https://t.me/+pack")
        await uow.daily_subscriptions.record_first_interaction(user_id, at=T0)
        if activate:
            await uow.daily_subscriptions.activate(user_id, at=T0, anchor_minute=NOON)


async def _library(uow_factory: SqlAlchemyUnitOfWorkFactory, count: int) -> list[DailyContent]:
    items = []
    async with uow_factory() as uow:
        existing = await uow.daily_content.count_active()
        for number in range(existing + 1, existing + count + 1):
            item, _ = await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=1,
                    source_message_id=number + int(uuid4().int % 1_000_000) * 1000,
                    added_by=1,
                    file_id=f"file-{uuid4().hex}",
                    file_unique_id=f"unique-{uuid4().hex}",
                )
            )
            items.append(item)
    return items


async def _subscription(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    user_id: int = BUYER,
) -> DailySubscription:
    async with uow_factory() as uow:
        found = await uow.daily_subscriptions.get(user_id)
    assert found is not None
    return found


async def _due_today(
    engine: DailyDeliveryService,
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    user_id: int = BUYER,
) -> DailyDelivery:
    delivery = await engine.open_due(await _subscription(uow_factory, user_id), now=T0)
    assert delivery is not None
    return delivery


async def _delivery(uow_factory: SqlAlchemyUnitOfWorkFactory, delivery_id: int) -> DailyDelivery:
    async with uow_factory() as uow:
        found = await uow.daily_deliveries.get(delivery_id)
    assert found is not None
    return found


async def _attempts(uow_factory: SqlAlchemyUnitOfWorkFactory, delivery_id: int) -> list[Any]:
    async with uow_factory() as uow:
        return list(await uow.daily_deliveries.attempts_of(delivery_id))


async def _available(uow_factory: SqlAlchemyUnitOfWorkFactory, user_id: int = BUYER) -> set[int]:
    """Items the buyer could still be given: active and not held or received."""
    async with uow_factory() as uow:
        session = uow.session
        from sqlalchemy import text  # noqa: PLC0415 — the one raw query here

        rows = await session.execute(
            text(
                "SELECT c.id FROM daily_content c WHERE c.status = 'active' AND NOT EXISTS ("
                " SELECT 1 FROM daily_deliveries d WHERE d.user_id = :user"
                " AND d.content_id = c.id AND d.status IN ('pending', 'sending', 'sent'))"
            ),
            {"user": user_id},
        )
        return {row[0] for row in rows}


# --------------------------------------------------------------------------- #
# The day's item                                                              #
# --------------------------------------------------------------------------- #


async def test_the_activation_item_goes_out_at_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 3)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock)

    opened = await engine.open_activation(BUYER, at=T0)
    assert opened is not None
    report = await engine.deliver(opened.id)

    assert report.step is DeliveryStep.SENT
    assert not report.recovered
    delivery = await _delivery(live_uow_factory, opened.id)
    assert delivery.status is DailyDeliveryStatus.SENT
    assert delivery.trigger is DailyDeliveryTrigger.ACTIVATION
    assert delivery.delivery_date == date(2026, 10, 10)
    assert telegram.accepted == [(BUYER, report.content_id)]
    assert telegram.protect == [True]
    assert report.content_id not in await _available(live_uow_factory)


async def test_one_item_a_day_whatever_runs_twice(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 5)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    opened = await engine.open_activation(BUYER, at=T0)
    assert opened is not None
    await engine.deliver(opened.id)

    # The planner for today, a second deliver, two more passes: nothing more today.
    subscription = await _subscription(live_uow_factory)
    assert await engine.open_due(subscription, now=T0 + timedelta(minutes=30)) is None
    again = await engine.deliver(opened.id)
    clock.advance(hours=1)
    assert await engine.process_due() == ()
    assert await engine.process_due() == ()

    assert again.step is DeliveryStep.ALREADY_SENT
    assert len(telegram.accepted) == 1

    # Tomorrow, around the same time, the next item.
    clock.now = T0 + timedelta(days=1)
    tomorrow = await engine.open_due(subscription, now=clock.now)
    assert tomorrow is not None
    (report,) = await engine.process_due()
    assert report.step is DeliveryStep.SENT
    first, second = telegram.accepted
    assert first[1] != second[1]


async def test_a_scheduled_delivery_is_due_at_the_buyers_own_time(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    engine = _engine(live_uow_factory, ScriptedTelegram(), Clock())
    subscription = await _subscription(live_uow_factory)

    assert await engine.open_due(subscription, now=T0 - timedelta(hours=2)) is None
    early = await engine.open_due(subscription, now=T0 - timedelta(minutes=40))
    assert early is not None
    assert early.next_attempt_at == T0  # opened early, due at the centre
    assert early.window_opens_at == T0 - timedelta(hours=1)
    assert early.window_closes_at == T0 + timedelta(hours=1)


async def test_a_late_window_is_recovered_at_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The worker was down through the whole window: the item still goes today."""
    await _subscriber(live_uow_factory)
    engine = _engine(live_uow_factory, ScriptedTelegram(), Clock())
    late = T0 + timedelta(hours=5)

    delivery = await engine.open_due(await _subscription(live_uow_factory), now=late)

    assert delivery is not None
    assert delivery.delivery_date == date(2026, 10, 10)
    assert delivery.next_attempt_at == late


# --------------------------------------------------------------------------- #
# The crash: Telegram took it, the process died before recording it          #
# --------------------------------------------------------------------------- #


async def test_a_crash_after_telegram_took_the_item_is_recovered_with_the_same_item(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 4)
    telegram, clock = ScriptedTelegram(script=[CRASH_AFTER_ACCEPT]), Clock()
    dying_worker = _engine(live_uow_factory, telegram, clock)

    # 1. The delivery is created.
    delivery = await _due_today(dying_worker, live_uow_factory)
    # 2-4. A worker starts sending, Telegram takes it, the process dies.
    with pytest.raises(ProcessDied):
        await dying_worker.process_due()
    (shown,) = telegram.accepted
    stuck = await _delivery(live_uow_factory, delivery.id)
    assert stuck.status is DailyDeliveryStatus.SENDING
    assert stuck.content_id == shown[1]
    (in_flight,) = await _attempts(live_uow_factory, delivery.id)
    assert in_flight.finished_at is None

    # While the lease lives, nobody touches it.
    next_worker = _engine(live_uow_factory, telegram, clock)
    clock.advance(seconds=LEASE.total_seconds() - 1)
    assert await next_worker.process_due() == ()

    # 5-7. The lease runs out; the next worker recovers it and sends again.
    clock.advance(seconds=2)
    (report,) = await next_worker.process_due()
    assert report.step is DeliveryStep.SENT
    assert report.recovered
    assert report.content_id == shown[1]

    # 8. The history says what happened: a crash, then the resend.
    recovered = await _delivery(live_uow_factory, delivery.id)
    assert recovered.status is DailyDeliveryStatus.SENT
    assert recovered.attempts == 2
    crashed, resent = await _attempts(live_uow_factory, delivery.id)
    assert crashed.outcome is DailyAttemptOutcome.CRASHED
    assert crashed.finished_at is not None
    assert crashed.error is not None
    assert resent.outcome is DailyAttemptOutcome.SENT
    assert resent.content_id == crashed.content_id
    # The buyer may have seen the item twice — never two different items.
    assert telegram.accepted == [(BUYER, shown[1]), (BUYER, shown[1])]
    # It is spent: no later day offers it again.
    assert shown[1] not in await _available(live_uow_factory)


async def test_the_dead_attempt_reporting_late_is_recorded_not_applied(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The first worker was only slow, not dead: both sends are visible."""
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 2)
    clock = Clock()
    slow = ScriptedTelegram(hold=asyncio.Event())
    fast = ScriptedTelegram()
    first_worker = _engine(live_uow_factory, slow, clock)
    second_worker = _engine(live_uow_factory, fast, clock)
    delivery = await _due_today(first_worker, live_uow_factory)

    stalled = asyncio.create_task(first_worker.process_due())
    await slow.started.wait()
    clock.advance(seconds=LEASE.total_seconds() + 1)
    (taken_over,) = await second_worker.process_due()
    assert slow.hold is not None
    slow.hold.set()
    (late,) = await stalled

    assert taken_over.step is DeliveryStep.SENT
    assert taken_over.recovered
    assert late.step is DeliveryStep.LEASE_LOST
    final = await _delivery(live_uow_factory, delivery.id)
    assert final.status is DailyDeliveryStatus.SENT
    first, second = await _attempts(live_uow_factory, delivery.id)
    assert first.outcome is DailyAttemptOutcome.LEASE_LOST
    assert first.telegram_message_id is not None  # the duplicate, on record
    assert second.outcome is DailyAttemptOutcome.SENT
    assert slow.accepted[0][1] == fast.accepted[0][1]  # the same item both times


async def test_an_expired_lease_is_never_sent_on(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """A slow batch: by the second item its lease is gone — it is handed back, not sent."""
    await _subscriber(live_uow_factory, 9970)
    await _subscriber(live_uow_factory, 9971)
    await _library(live_uow_factory, 2)
    clock = Clock()
    telegram = ScriptedTelegram()
    engine = _engine(live_uow_factory, telegram, clock)
    first = await _due_today(engine, live_uow_factory, 9970)
    second = await _due_today(engine, live_uow_factory, 9971)
    telegram.before_send = lambda: clock.advance(seconds=LEASE.total_seconds() + 1)

    reports = await engine.process_due()

    by_delivery = {report.delivery_id: report.step for report in reports}
    assert by_delivery == {
        first.id: DeliveryStep.SENT,
        second.id: DeliveryStep.RETRY_SCHEDULED,
    }
    assert len(telegram.calls) == 1
    handed_back = await _delivery(live_uow_factory, second.id)
    assert handed_back.status is DailyDeliveryStatus.PENDING
    assert handed_back.content_id is not None  # the same item stays due
    (attempt,) = await _attempts(live_uow_factory, second.id)
    assert attempt.outcome is DailyAttemptOutcome.TRANSIENT

    telegram.before_send = None
    (sent,) = await engine.process_due()
    assert sent.delivery_id == second.id
    assert sent.content_id == handed_back.content_id


@pytest.mark.parametrize(
    "late_failure",
    [
        DailySendTransientError("Request timeout error"),
        DailySendBlockedError("Forbidden: bot was blocked by the user"),
        DailySendRejectedError("Bad Request: something new"),
        DailyContentRejectedError("Bad Request: wrong file identifier"),
    ],
    ids=["uncertain", "blocked", "refused", "item-refused"],
)
async def test_a_late_failure_never_undoes_what_the_successor_sent(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    late_failure: Exception,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 2)
    clock = Clock()
    slow = ScriptedTelegram(script=[late_failure], hold=asyncio.Event())
    first_worker = _engine(live_uow_factory, slow, clock)
    second_worker = _engine(live_uow_factory, ScriptedTelegram(), clock)
    delivery = await _due_today(first_worker, live_uow_factory)

    stalled = asyncio.create_task(first_worker.process_due())
    await slow.started.wait()
    clock.advance(seconds=LEASE.total_seconds() + 1)
    (taken_over,) = await second_worker.process_due()
    assert slow.hold is not None
    slow.hold.set()
    (late,) = await stalled

    assert taken_over.step is DeliveryStep.SENT
    assert late.step is DeliveryStep.LEASE_LOST
    final = await _delivery(live_uow_factory, delivery.id)
    assert final.status is DailyDeliveryStatus.SENT
    assert final.content_id == taken_over.content_id
    first, second = await _attempts(live_uow_factory, delivery.id)
    assert first.outcome is DailyAttemptOutcome.LEASE_LOST
    assert second.outcome is DailyAttemptOutcome.SENT


async def test_deliver_leaves_alone_what_it_may_not_attempt(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    opened = await engine.open_activation(BUYER, at=T0)
    assert opened is not None
    async with live_uow_factory() as uow:
        await uow.daily_deliveries.start_attempt(
            opened.id,
            content_id=item.id,
            now=T0,
            lease_until=T0 + LEASE,
        )

    busy = await engine.deliver(opened.id)
    unknown = await engine.deliver(10_000_000)

    assert busy.step is DeliveryStep.NOT_DUE
    assert unknown.step is DeliveryStep.NOT_DUE
    assert telegram.calls == []


async def test_refusals_up_to_the_last_attempt_give_the_day_up(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    refused = DailySendRejectedError("Bad Request: something new")
    telegram, clock = ScriptedTelegram(script=[refused]), Clock()
    engine = _engine(live_uow_factory, telegram, clock, max_attempts=1)
    delivery = await _due_today(engine, live_uow_factory)

    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.FAILED
    assert (await _delivery(live_uow_factory, delivery.id)).status is DailyDeliveryStatus.FAILED
    assert item.id in await _available(live_uow_factory)  # definitely not delivered


# --------------------------------------------------------------------------- #
# Telegram definitely refused: the item stays the buyer's                     #
# --------------------------------------------------------------------------- #


async def test_a_blocked_bot_closes_the_day_and_keeps_the_item(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    blocked = DailySendBlockedError("Forbidden: bot was blocked by the user")
    telegram, clock = ScriptedTelegram(script=[blocked]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)

    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.BLOCKED
    closed = await _delivery(live_uow_factory, delivery.id)
    assert closed.status is DailyDeliveryStatus.FAILED
    assert closed.last_outcome is DailyAttemptOutcome.BLOCKED
    assert (await _subscription(live_uow_factory)).is_blocked
    assert telegram.accepted == []
    assert item.id in await _available(live_uow_factory)  # never delivered
    # No retries for a blocked buyer, and no new days.
    clock.advance(hours=1)
    assert await engine.process_due() == ()
    clock.now = T0 + timedelta(days=1)
    assert await engine.open_due(await _subscription(live_uow_factory), now=clock.now) is None


async def test_a_refused_message_gives_the_item_back(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    refused = DailySendRejectedError("Bad Request: something new")
    telegram, clock = ScriptedTelegram(script=[refused]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)

    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.ITEM_RELEASED
    open_again = await _delivery(live_uow_factory, delivery.id)
    assert open_again.status is DailyDeliveryStatus.PENDING
    assert open_again.content_id is None
    (attempt,) = await _attempts(live_uow_factory, delivery.id)
    assert attempt.outcome is DailyAttemptOutcome.PERMANENT
    assert item.id in await _available(live_uow_factory)
    async with live_uow_factory() as uow:
        still = await uow.daily_content.get(item.id)
    assert still is not None
    assert still.status is DailyContentStatus.ACTIVE  # not the item's fault

    # The day goes on: the next pass sends.
    (sent,) = await engine.process_due()
    assert sent.step is DeliveryStep.SENT


async def test_a_refused_item_is_taken_out_and_another_goes_the_same_day(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 2)
    broken = DailyContentRejectedError("Bad Request: wrong file identifier")
    telegram, clock = ScriptedTelegram(script=[broken]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    opened = await engine.open_activation(BUYER, at=T0)
    assert opened is not None

    report = await engine.deliver(opened.id)

    assert report.step is DeliveryStep.SENT
    (refused_item, _), (sent_item, _) = [(c, b) for b, c in telegram.calls]
    assert refused_item != sent_item
    async with live_uow_factory() as uow:
        refused = await uow.daily_content.get(refused_item)
    assert refused is not None
    assert refused.status is DailyContentStatus.DISABLED
    assert refused.status_reason is not None
    assert "wrong file identifier" in refused.status_reason
    first, second = await _attempts(live_uow_factory, opened.id)
    assert first.outcome is DailyAttemptOutcome.CONTENT_INVALID
    assert second.outcome is DailyAttemptOutcome.SENT


# --------------------------------------------------------------------------- #
# Temporary failures                                                          #
# --------------------------------------------------------------------------- #


async def test_flood_control_retries_the_same_item_after_telegrams_wait(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 3)
    flood = DailySendTransientError("Too Many Requests", retry_after=40)
    telegram, clock = ScriptedTelegram(script=[flood]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)

    (first,) = await engine.process_due()

    assert first.step is DeliveryStep.RETRY_SCHEDULED
    assert first.retry_after == 40
    waiting = await _delivery(live_uow_factory, delivery.id)
    assert waiting.status is DailyDeliveryStatus.PENDING
    assert waiting.next_attempt_at == T0 + timedelta(seconds=40)
    assert waiting.content_id == first.content_id

    clock.advance(seconds=39)
    assert await engine.process_due() == ()
    clock.advance(seconds=1)
    (second,) = await engine.process_due()
    assert second.step is DeliveryStep.SENT
    assert second.content_id == first.content_id
    failed, sent = await _attempts(live_uow_factory, delivery.id)
    assert (failed.outcome, failed.retry_after_seconds) == (DailyAttemptOutcome.TRANSIENT, 40)
    assert sent.outcome is DailyAttemptOutcome.SENT


async def test_a_timeout_retries_the_same_item_with_back_off(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 3)
    timeout = DailySendTransientError("Request timeout error")
    telegram, clock = ScriptedTelegram(script=[timeout, timeout]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)

    await engine.process_due()
    assert (await _delivery(live_uow_factory, delivery.id)).next_attempt_at == T0 + timedelta(
        seconds=30
    )
    clock.advance(seconds=30)
    await engine.process_due()
    assert (
        await _delivery(live_uow_factory, delivery.id)
    ).next_attempt_at == clock.now + timedelta(seconds=60)
    clock.advance(seconds=60)
    (sent,) = await engine.process_due()

    assert sent.step is DeliveryStep.SENT
    assert len({item for _, item in telegram.calls}) == 1  # one item, three tries


async def test_an_unexpected_transport_failure_counts_as_uncertain(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 2)
    telegram, clock = ScriptedTelegram(script=[RuntimeError("socket on fire")]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)

    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.RETRY_SCHEDULED
    assert (await _delivery(live_uow_factory, delivery.id)).content_id == report.content_id


async def test_after_the_last_attempt_the_day_is_given_up(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    down = DailySendTransientError("Telegram is unavailable")
    telegram, clock = ScriptedTelegram(script=[down, down]), Clock()
    engine = _engine(live_uow_factory, telegram, clock, max_attempts=2)
    delivery = await _due_today(engine, live_uow_factory)

    await engine.process_due()
    clock.advance(minutes=5)
    (last,) = await engine.process_due()

    assert last.step is DeliveryStep.FAILED
    assert (await _delivery(live_uow_factory, delivery.id)).status is DailyDeliveryStatus.FAILED
    assert item.id in await _available(live_uow_factory)


async def test_a_retry_past_the_deadline_expires(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    telegram = ScriptedTelegram(script=[DailySendTransientError("down")])
    clock = Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)
    await engine.process_due()

    clock.now = delivery.deadline_at
    assert await engine.process_due() == ()
    assert await engine.expire_overdue() == 1

    assert (await _delivery(live_uow_factory, delivery.id)).status is DailyDeliveryStatus.EXPIRED
    assert item.id in await _available(live_uow_factory)


# --------------------------------------------------------------------------- #
# Nothing to send, nobody to send to                                          #
# --------------------------------------------------------------------------- #


async def test_a_buyer_with_everything_received_waits_for_new_items(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (only,) = await _library(live_uow_factory, 1)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    first = await engine.open_activation(BUYER, at=T0)
    assert first is not None
    await engine.deliver(first.id)

    clock.now = T0 + timedelta(days=1)
    tomorrow = await engine.open_due(await _subscription(live_uow_factory), now=clock.now)
    assert tomorrow is not None
    (report,) = await engine.process_due()
    assert report.step is DeliveryStep.NO_CONTENT
    assert telegram.accepted == [(BUYER, only.id)]  # never the old item again

    (fresh,) = await _library(live_uow_factory, 1)
    clock.advance(seconds=900)
    (sent,) = await engine.process_due()
    assert sent.step is DeliveryStep.SENT
    assert sent.content_id == fresh.id


@pytest.mark.parametrize("why", ["paused", "blocked", "below-15", "outside-beta"])
async def test_a_buyer_who_may_not_receive_gets_nothing(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    why: str,
) -> None:
    await _subscriber(live_uow_factory, completed=14 if why == "below-15" else 15)
    await _library(live_uow_factory, 2)
    telegram, clock = ScriptedTelegram(), Clock()
    beta = {"beta_user_ids": (BUYER + 1,)} if why == "outside-beta" else {}
    engine = _engine(live_uow_factory, telegram, clock, **beta)
    delivery = await _due_today(engine, live_uow_factory)
    async with live_uow_factory() as uow:
        if why == "paused":
            await uow.daily_subscriptions.pause(BUYER, at=T0)
        if why == "blocked":
            await uow.daily_subscriptions.mark_blocked(BUYER, at=T0)

    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.CANCELLED
    assert (await _delivery(live_uow_factory, delivery.id)).status is (
        DailyDeliveryStatus.CANCELLED
    )
    assert telegram.calls == []


async def test_a_dead_attempt_of_a_buyer_who_paused_is_closed_not_resent(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 1)
    telegram, clock = ScriptedTelegram(script=[CRASH_AFTER_ACCEPT]), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    delivery = await _due_today(engine, live_uow_factory)
    with pytest.raises(ProcessDied):
        await engine.process_due()
    async with live_uow_factory() as uow:
        await uow.daily_subscriptions.pause(BUYER, at=T0)

    clock.advance(seconds=LEASE.total_seconds() + 1)
    (report,) = await engine.process_due()

    assert report.step is DeliveryStep.CANCELLED
    assert len(telegram.accepted) == 1
    (crashed,) = await _attempts(live_uow_factory, delivery.id)
    assert crashed.outcome is DailyAttemptOutcome.CRASHED


async def test_nothing_happens_while_daily_content_is_off(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 1)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock, enabled=False)
    delivery = await _due_today(engine, live_uow_factory)

    assert await engine.process_due() == ()
    assert (await engine.deliver(delivery.id)).step is DeliveryStep.DISABLED
    assert telegram.calls == []


# --------------------------------------------------------------------------- #
# Two workers                                                                 #
# --------------------------------------------------------------------------- #


async def test_two_workers_send_every_delivery_exactly_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    buyers = list(range(9960, 9968))
    for user_id in buyers:
        await _subscriber(live_uow_factory, user_id)
    await _library(live_uow_factory, 3)
    clock = Clock()
    telegram = ScriptedTelegram()
    planner = _engine(live_uow_factory, telegram, clock)
    for user_id in buyers:
        await _due_today(planner, live_uow_factory, user_id)

    first = _engine(live_uow_factory, telegram, clock)
    second = _engine(live_uow_factory, telegram, clock)
    reports = await asyncio.gather(first.process_due(limit=5), second.process_due(limit=5))

    sent = [report for batch in reports for report in batch]
    assert sorted(report.user_id for report in sent if report.user_id) == buyers
    assert all(report.step is DeliveryStep.SENT for report in sent)
    assert sorted(user for user, _ in telegram.accepted) == buyers  # once each


async def test_two_immediate_sends_of_one_delivery_send_it_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """A double tap on activation: the row lock and the lease let one through."""
    await _subscriber(live_uow_factory)
    await _library(live_uow_factory, 3)
    telegram, clock = ScriptedTelegram(), Clock()
    engine = _engine(live_uow_factory, telegram, clock)
    opened = await engine.open_activation(BUYER, at=T0)
    assert opened is not None

    reports = await asyncio.gather(*(engine.deliver(opened.id) for _ in range(5)))

    steps = sorted(report.step.value for report in reports)
    assert steps.count(DeliveryStep.SENT.value) == 1
    assert len(telegram.accepted) == 1
