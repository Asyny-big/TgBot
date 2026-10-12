"""The Daily worker end to end, on PostgreSQL, over several simulated days.

Real loops, services, repositories and Telegram adapters; only Telegram itself
is a recording double — one for the Daily bot, one for the shop bot. Time is
simulated twice over: the business clock (when buyers' windows open) and the
monotonic clock the sending budget counts in. Nothing really sleeps.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage, SendVideo
from sqlalchemy import text

from app.core.config import DailySettings
from app.daily_bot import texts
from app.daily_worker.health import Heartbeats
from app.daily_worker.loops import EvaluatorLoop, ExpiryLoop, OutboxLoop, PlannerLoop, SenderLoop
from app.domain.commands import DailyContentDraft, DailyDeliveryDraft, DailyRequestDraft
from app.domain.daily import DAILY_TIMEZONE, activation_window
from app.domain.daily_alerts import AlertNotice, AlertObservation, TokenState
from app.domain.enums import (
    DailyAlertKey,
    DailyAlertState,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxChannel,
    DailyOutboxStatus,
    DailyRequestEventKind,
)
from app.infrastructure.telegram.daily import TelegramDailyContentSender
from app.infrastructure.telegram.daily_pacing import PacedDailySender, PacedTextSender, SendPacer
from app.infrastructure.telegram.daily_runtime import TelegramTextSender
from app.services.daily_admin import DailyAdminService
from app.services.daily_alerts import DailyAlertService
from app.services.daily_delivery import DailyDeliveryService
from app.services.daily_library import DailyLibraryService
from app.services.daily_notices import DailyNoticeService
from app.services.daily_outbox import DailyOutboxService
from app.services.daily_planning import DailyPlanningService
from app.services.daily_stock import DailyStockService
from app.services.daily_subscriptions import ContactOutcome, DailySubscriptionService
from tests.concurrency import Gate, all_at_once, gated
from tests.daily_bot_harness import DailyRecordingBot, seed_buyer

if TYPE_CHECKING:
    from app.domain.entities import DailyDelivery
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

OWNER = 9500
FIRST_DAY = date(2026, 10, 12)
ROUNDS_CAP = 200


def moscow(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment of ``FIRST_DAY + day`` on the Moscow clock, in UTC."""
    local = datetime.combine(
        FIRST_DAY + timedelta(days=day),
        time(hour % 24, minute),
        tzinfo=DAILY_TIMEZONE,
    )
    return local.astimezone(UTC) + timedelta(days=hour // 24)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeTime:
    """The sending budget's clock: moves with the business clock, never sleeps."""

    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class StubProbe:
    state: TokenState = TokenState.OK

    async def check(self) -> TokenState:
        return self.state


@dataclass
class Worker:
    """One Daily worker over the live database, driven by hand."""

    uow_factory: SqlAlchemyUnitOfWorkFactory
    settings: DailySettings
    clock: Clock
    time: FakeTime
    bot: DailyRecordingBot
    shop: DailyRecordingBot
    pacer: SendPacer
    delivery: DailyDeliveryService
    probe: StubProbe
    planner: PlannerLoop
    sender: SenderLoop
    outbox: OutboxLoop
    expiry: ExpiryLoop
    evaluator: EvaluatorLoop

    def at(self, moment: datetime) -> None:
        """Move both clocks forward to ``moment``."""
        step = (moment - self.clock.now).total_seconds()
        assert step >= 0, "time only moves forward"
        self.clock.now = moment
        self.time.now += step

    def advance(self, **delta: float) -> None:
        self.at(self.clock.now + timedelta(**delta))

    async def send_all(self) -> None:
        """Turn the sending loops until nothing more is due right now."""
        for loop in (self.sender, self.outbox):
            for _ in range(ROUNDS_CAP):
                result = await loop.run_once()
                if result.paused or result.claimed == 0:
                    break

    async def tick(self) -> None:
        """One turn of every loop at the current time."""
        await self.planner.run_once()
        await self.send_all()
        await self.expiry.run_once()

    async def run_days(self, first: int, last: int, *, step_minutes: int = 30) -> None:
        """Tick through whole Moscow days, every ``step_minutes``."""
        moment = moscow(first, 0)
        end = moscow(last + 1, 0)
        while moment <= end:
            self.at(moment)
            await self.tick()
            moment += timedelta(minutes=step_minutes)

    def library(self) -> DailyLibraryService:
        return DailyLibraryService(
            uow_factory=self.uow_factory,
            settings=self.settings,
            sender=TelegramDailyContentSender(self.bot),
            clock=self.clock,
        )

    def subscriptions(self) -> DailySubscriptionService:
        return DailySubscriptionService(
            uow_factory=self.uow_factory,
            settings=self.settings,
            clock=self.clock,
        )


Factory = Callable[..., Worker]


@pytest.fixture
async def make_worker(live_uow_factory: SqlAlchemyUnitOfWorkFactory) -> AsyncIterator[Factory]:
    built: list[Worker] = []

    def build(**overrides: Any) -> Worker:
        values: dict[str, Any] = {
            "enabled": True,
            "admin_telegram_ids": (OWNER,),
            "lease_seconds": 300,
            "recovery_hours": 12,
            "max_attempts": 5,
            "retry_initial_seconds": 30,
            "retry_max_seconds": 900,
            "no_content_recheck_seconds": 900,
            "send_batch_size": 10,
            "send_rate_per_second": 20,
            "low_stock_days": 3,
            "alert_cooldown_hours": 24,
            "activation_grace_seconds": 120,
        }
        values.update(overrides)
        settings = DailySettings(**values)
        clock, fake_time = Clock(moscow(0, 0)), FakeTime()
        bot, shop = DailyRecordingBot(), DailyRecordingBot()
        pacer = SendPacer(
            rate_per_second=settings.send_rate_per_second,
            clock=fake_time,
            sleep=fake_time.sleep,
        )
        heartbeats = Heartbeats(clock=fake_time)
        items = PacedDailySender(TelegramDailyContentSender(bot), pacer)
        delivery = DailyDeliveryService(
            uow_factory=live_uow_factory,
            sender=items,
            settings=settings,
            clock=clock,
            jitter=lambda: 0.0,
        )
        outbox = DailyOutboxService(
            uow_factory=live_uow_factory,
            settings=settings,
            senders={
                DailyOutboxChannel.DAILY: PacedTextSender(TelegramTextSender(bot), pacer),
                DailyOutboxChannel.SHOP: TelegramTextSender(shop),
            },
            item_sender=items,
            clock=clock,
            jitter=lambda: 0.0,
        )
        probe = StubProbe()
        worker = Worker(
            uow_factory=live_uow_factory,
            settings=settings,
            clock=clock,
            time=fake_time,
            bot=bot,
            shop=shop,
            pacer=pacer,
            delivery=delivery,
            probe=probe,
            planner=PlannerLoop(
                planning=DailyPlanningService(
                    uow_factory=live_uow_factory,
                    settings=settings,
                    clock=clock,
                ),
                settings=settings,
                heartbeats=heartbeats,
            ),
            sender=SenderLoop(
                delivery=delivery,
                notices=DailyNoticeService(
                    uow_factory=live_uow_factory,
                    settings=settings,
                    clock=clock,
                ),
                settings=settings,
                pacer=pacer,
                heartbeats=heartbeats,
            ),
            outbox=OutboxLoop(outbox=outbox, settings=settings, pacer=pacer, heartbeats=heartbeats),
            expiry=ExpiryLoop(
                delivery=delivery,
                outbox=outbox,
                settings=settings,
                heartbeats=heartbeats,
            ),
            evaluator=EvaluatorLoop(
                stock=DailyStockService(
                    uow_factory=live_uow_factory,
                    settings=settings,
                    clock=clock,
                ),
                alerts=DailyAlertService(
                    uow_factory=live_uow_factory,
                    settings=settings,
                    clock=clock,
                ),
                probe=probe,
                settings=settings,
                heartbeats=heartbeats,
            ),
        )
        built.append(worker)
        return worker

    yield build
    for worker in built:
        await worker.bot.session.close()
        await worker.shop.session.close()


@pytest.fixture
def worker(make_worker: Factory) -> Worker:
    return make_worker()


# --------------------------------------------------------------------------- #
# Seeding and reading                                                         #
# --------------------------------------------------------------------------- #


async def _subscriber(
    worker: Worker,
    user_id: int,
    *,
    anchor: int,
    activation_sent: bool = False,
) -> None:
    """A buyer who activated the day before the first simulated day, at ``anchor``.

    ``activation_sent`` also records that day's activation item as received —
    what the bot did then. Without it, a late anchor's window of that day is
    still open for recovery when the simulation starts.
    """
    await seed_buyer(worker.uow_factory, user_id)
    activated = moscow(-1, anchor // 60, anchor % 60)
    async with worker.uow_factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=activated)
        await uow.daily_subscriptions.activate(user_id, at=activated, anchor_minute=anchor)
        if not activation_sent:
            return
        delivery = await uow.daily_deliveries.open(
            DailyDeliveryDraft(
                user_id=user_id,
                trigger=DailyDeliveryTrigger.ACTIVATION,
                window=activation_window(activated, recovery=worker.settings.recovery),
                first_attempt_at=activated,
            )
        )
        item = await uow.daily_content.pick_for(user_id)
        assert delivery is not None
        assert item is not None
        attempt = await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=item.id,
            now=activated,
            lease_until=activated + timedelta(minutes=5),
        )
        await uow.daily_deliveries.finish_sent(
            attempt.lease_token,
            at=activated,
            telegram_message_id=1,
        )


async def _library(worker: Worker, count: int) -> list[int]:
    ids = []
    async with worker.uow_factory() as uow:
        for _ in range(count):
            item, _ = await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=OWNER,
                    source_message_id=int(uuid4().int % 100_000_000),
                    added_by=OWNER,
                    file_id=f"video-{uuid4().hex}",
                    file_unique_id=f"unique-{uuid4().hex}",
                )
            )
            ids.append(item.id)
    return ids


async def _deliveries(worker: Worker, user_id: int | None = None) -> list[DailyDelivery]:
    where = "" if user_id is None else "WHERE user_id = :user"
    async with worker.uow_factory() as uow:
        rows = await uow.session.execute(
            text(f"SELECT id FROM daily_deliveries {where} ORDER BY id"),  # noqa: S608 — fixed text
            {"user": user_id},
        )
        found = [await uow.daily_deliveries.get(row[0]) for row in rows]
    return [delivery for delivery in found if delivery is not None]


async def _count(worker: Worker, table: str) -> int:
    async with worker.uow_factory() as uow:
        return int(
            (await uow.session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        )


def _videos_to(worker: Worker, chat_id: int) -> list[str]:
    return [str(call.video) for call in worker.bot.items_to(chat_id) if isinstance(call, SendVideo)]


def _video(chat_id: int) -> SendVideo:
    return SendVideo(chat_id=chat_id, video="x")


# --------------------------------------------------------------------------- #
# Every day, one item                                                         #
# --------------------------------------------------------------------------- #


async def test_three_buyers_get_one_new_item_a_day_at_their_own_time(worker: Worker) -> None:
    anchors = {9501: 9 * 60 + 10, 9502: 12 * 60 + 35, 9503: 23 * 60 + 50}
    await _library(worker, 12)
    for user_id, anchor in anchors.items():
        await _subscriber(worker, user_id, anchor=anchor, activation_sent=True)

    await worker.run_days(0, 2)

    for user_id, anchor in anchors.items():
        videos = _videos_to(worker, user_id)
        assert len(videos) == 3, user_id
        assert len(set(videos)) == 3, user_id  # never the same item twice
        activation, *deliveries = await _deliveries(worker, user_id)
        assert activation.delivery_date == FIRST_DAY - timedelta(days=1)
        assert [delivery.delivery_date for delivery in deliveries] == [
            FIRST_DAY + timedelta(days=day) for day in range(3)
        ]
        assert activation.content_id not in {delivery.content_id for delivery in deliveries}
        for day, delivery in enumerate(deliveries):
            assert delivery.status is DailyDeliveryStatus.SENT
            assert delivery.sent_at is not None
            due = moscow(day, anchor // 60, anchor % 60)
            assert due <= delivery.sent_at < due + timedelta(minutes=30)


async def test_a_temporary_failure_is_retried_the_same_day(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    await _library(worker, 3)
    worker.bot.failures[SendVideo] = [
        TelegramNetworkError(method=_video(9501), message="Request timeout error"),
    ]
    worker.at(moscow(0, 12))

    await worker.tick()
    assert _videos_to(worker, 9501) == []
    worker.advance(seconds=31)
    await worker.tick()

    assert len(_videos_to(worker, 9501)) == 1
    (delivery,) = await _deliveries(worker, 9501)
    assert delivery.status is DailyDeliveryStatus.SENT
    assert delivery.attempts == 2


async def test_an_attempt_that_died_is_taken_over_with_the_same_item(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    (item, *_) = await _library(worker, 3)
    worker.at(moscow(0, 11, 30))
    await worker.planner.run_once()
    (opened,) = await _deliveries(worker, 9501)
    worker.at(moscow(0, 12))
    async with worker.uow_factory() as uow:  # a worker claims it, then dies mid-send
        await uow.daily_deliveries.start_attempt(
            opened.id,
            content_id=item,
            now=worker.clock.now,
            lease_until=worker.clock.now + timedelta(minutes=5),
        )

    await worker.tick()
    assert _videos_to(worker, 9501) == []  # the lease still holds it
    worker.advance(minutes=6)
    await worker.tick()

    assert len(_videos_to(worker, 9501)) == 1
    async with worker.uow_factory() as uow:
        delivery = await uow.daily_deliveries.get(opened.id)
        attempts = await uow.daily_deliveries.attempts_of(opened.id)
    assert delivery is not None
    assert delivery.status is DailyDeliveryStatus.SENT
    assert delivery.content_id == item
    assert [attempt.outcome for attempt in attempts] == [
        DailyAttemptOutcome.CRASHED,
        DailyAttemptOutcome.SENT,
    ]


async def test_flood_control_pauses_the_whole_worker(worker: Worker) -> None:
    for user_id in (9501, 9502, 9503):
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 6)
    worker.bot.failures[SendVideo] = [
        TelegramRetryAfter(
            method=_video(9501),
            message="Too Many Requests: retry after 30",
            retry_after=30,
        ),
    ]
    worker.at(moscow(0, 12))

    await worker.tick()

    # One request reached Telegram; the other two were held back, not sent.
    assert len([call for call in worker.bot.attempted if isinstance(call, SendVideo)]) == 1
    assert worker.pacer.paused_for() == pytest.approx(30)
    held = await worker.sender.run_once()
    assert held.paused

    worker.advance(seconds=31)
    await worker.tick()

    for user_id in (9501, 9502, 9503):
        assert len(_videos_to(worker, user_id)) == 1


# --------------------------------------------------------------------------- #
# Missed days, pauses, blocks                                                 #
# --------------------------------------------------------------------------- #


async def test_a_day_missed_while_the_worker_was_down_is_never_caught_up(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    await _library(worker, 5)
    worker.at(moscow(0, 12))
    await worker.tick()
    # Day 1: planned at 11:30, then the sender stopped until the next afternoon.
    worker.at(moscow(1, 11, 30))
    await worker.planner.run_once()

    worker.at(moscow(2, 12))
    await worker.tick()

    deliveries = await _deliveries(worker, 9501)
    assert [(delivery.delivery_date, delivery.status) for delivery in deliveries] == [
        (FIRST_DAY, DailyDeliveryStatus.SENT),
        (FIRST_DAY + timedelta(days=1), DailyDeliveryStatus.EXPIRED),
        (FIRST_DAY + timedelta(days=2), DailyDeliveryStatus.SENT),
    ]
    assert len(_videos_to(worker, 9501)) == 2  # one a day, never two to make up


async def test_paused_and_blocked_buyers_receive_nothing(worker: Worker) -> None:
    for user_id in (9501, 9502, 9503):
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 6)
    async with worker.uow_factory() as uow:
        await uow.daily_subscriptions.pause(9501, at=worker.clock.now)
        await uow.daily_subscriptions.mark_blocked(9502, at=worker.clock.now)

    await worker.run_days(0, 0)
    async with worker.uow_factory() as uow:
        await uow.daily_subscriptions.resume(9501, at=worker.clock.now)
    await worker.run_days(1, 1)

    assert len(_videos_to(worker, 9501)) == 1  # from the day after resuming
    assert _videos_to(worker, 9502) == []
    assert len(_videos_to(worker, 9503)) == 2


# --------------------------------------------------------------------------- #
# Activation: the bot's turn first, an empty library later filled             #
# --------------------------------------------------------------------------- #


async def test_a_fresh_activation_is_left_to_the_bot_for_two_minutes(worker: Worker) -> None:
    await seed_buyer(worker.uow_factory, 9501)
    await _library(worker, 3)
    worker.at(moscow(0, 15))
    contact = await worker.subscriptions().first_contact(9501)
    assert contact.outcome is ContactOutcome.ACTIVATED
    # The bot is sending the welcome right now; the worker must not jump in.

    worker.advance(seconds=60)
    await worker.send_all()
    assert _videos_to(worker, 9501) == []

    # The bot died before sending the item: the worker takes over.
    worker.advance(seconds=61)
    await worker.send_all()
    assert len(_videos_to(worker, 9501)) == 1


async def test_an_activation_on_an_empty_library_gets_its_item_once_there_is_one(
    worker: Worker,
) -> None:
    await seed_buyer(worker.uow_factory, 9501)
    worker.at(moscow(0, 15))
    contact = await worker.subscriptions().first_contact(9501)
    assert contact.delivery is not None
    report = await worker.delivery.deliver(contact.delivery.id)  # what the bot does
    assert report.step.value == "no_content"

    worker.advance(minutes=5)
    added = await worker.library().add(
        OWNER,
        DailyContentDraft(
            kind=DailyContentKind.VIDEO,
            source_chat_id=OWNER,
            source_message_id=1,
            added_by=OWNER,
            file_id="video-first",
            file_unique_id="unique-first",
        ),
    )
    assert added.item is not None
    await worker.tick()
    assert _videos_to(worker, 9501) == []  # looked at again only after 15 minutes
    worker.advance(minutes=11)
    await worker.tick()

    assert _videos_to(worker, 9501) == ["video-first"]
    assert worker.bot.texts_to(9501) == []  # never "you have seen everything"


async def test_two_workers_never_send_one_delivery_twice(make_worker: Factory) -> None:
    first, second = make_worker(), make_worker()
    buyers = range(9501, 9513)
    for user_id in buyers:
        await _subscriber(first, user_id, anchor=12 * 60)
    await _library(first, 12)
    for worker in (first, second):
        worker.at(moscow(0, 12))
    await first.planner.run_once()

    for _ in range(10):
        results = await all_at_once(first.sender.run_once(), second.sender.run_once())
        assert [result for result in results if isinstance(result, BaseException)] == []
        if all(result.claimed == 0 for result in results):
            break

    for user_id in buyers:
        sent = _videos_to(first, user_id) + _videos_to(second, user_id)
        assert len(sent) == 1, user_id


# --------------------------------------------------------------------------- #
# "You have seen everything"                                                  #
# --------------------------------------------------------------------------- #


async def test_a_buyer_who_has_seen_everything_is_told_once_per_episode(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    await _library(worker, 1)

    await worker.run_days(0, 1)  # day 0: the item; day 1: nothing left

    assert len(_videos_to(worker, 9501)) == 1
    assert worker.bot.texts_to(9501) == [
        "Вы посмотрели все материалы 🎉 Новые придут автоматически, как только появятся."
    ]
    (notice,) = [call for call in worker.bot.calls if isinstance(call, SendMessage)]
    assert notice.chat_id == 9501

    # A new item arrives: delivered, and the next time the library runs dry the
    # buyer is told again.
    await _library(worker, 1)
    await worker.run_days(2, 3)

    assert len(_videos_to(worker, 9501)) == 2
    assert worker.bot.texts_to(9501) == [texts.EXHAUSTED, texts.EXHAUSTED]


# --------------------------------------------------------------------------- #
# The owner's alerts                                                          #
# --------------------------------------------------------------------------- #


async def test_low_stock_is_reported_once_and_its_end_too(worker: Worker) -> None:
    for user_id in (9501, 9502):
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 2)
    worker.at(moscow(0, 9))

    for _ in range(3):  # the evaluator runs every ten minutes
        await worker.evaluator.run_once()
        await worker.send_all()
        worker.advance(minutes=10)

    (alert,) = worker.bot.texts_to(OWNER)
    assert alert.startswith("⚠️ Daily: материалов мало")

    await _library(worker, 30)
    await worker.evaluator.run_once()
    await worker.send_all()

    assert worker.bot.texts_to(OWNER)[1:] == ["✅ Daily: запас материалов снова в норме."]
    async with worker.uow_factory() as uow:
        state = await uow.daily_alerts.get(DailyAlertKey.LOW_STOCK)
    assert state is not None
    assert (state.state, state.notifications) == (DailyAlertState.OK, 2)


async def test_a_revoked_daily_token_is_reported_through_the_shop_bot(worker: Worker) -> None:
    worker.probe.state = TokenState.UNAUTHORIZED

    await worker.evaluator.run_once()
    await worker.send_all()
    worker.probe.state = TokenState.OK
    await worker.evaluator.run_once()
    await worker.send_all()

    shop_texts = worker.shop.texts_to(OWNER)
    assert len(shop_texts) == 2
    assert "401" in shop_texts[0]
    assert shop_texts[1].startswith("✅")
    assert worker.bot.texts_to(OWNER) == []  # the Daily bot could not have said it


async def test_failing_deliveries_raise_an_alert(make_worker: Factory) -> None:
    worker = make_worker(max_attempts=1)
    buyers = range(9501, 9507)
    for user_id in buyers:
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 6)
    worker.bot.failures[SendVideo] = [
        TelegramBadRequest(method=_video(user_id), message="Bad Request: something went wrong")
        for user_id in buyers
    ]
    worker.at(moscow(0, 12))
    await worker.tick()

    await worker.evaluator.run_once()
    await worker.send_all()

    statuses = Counter(delivery.status for delivery in await _deliveries(worker))
    assert statuses == {DailyDeliveryStatus.FAILED: 6}
    (alert,) = [message for message in worker.bot.texts_to(OWNER) if "не доставлено" in message]
    assert "6 из 6" in alert


# --------------------------------------------------------------------------- #
# Off means off                                                               #
# --------------------------------------------------------------------------- #


async def test_while_daily_content_is_off_the_worker_writes_and_sends_nothing(
    make_worker: Factory,
) -> None:
    on, off = make_worker(), make_worker(enabled=False)
    await _subscriber(on, 9501, anchor=12 * 60)
    await _library(on, 2)
    on.at(moscow(0, 11, 30))
    await on.planner.run_once()  # one delivery waiting, opened while it was on
    off.probe.state = TokenState.UNAUTHORIZED
    tables = ("daily_deliveries", "daily_delivery_attempts", "daily_outbox", "daily_alerts")
    before = {table: await _count(off, table) for table in tables}
    statuses_before = [delivery.status for delivery in await _deliveries(off)]

    await off.run_days(0, 1)
    await off.evaluator.run_once()

    assert {table: await _count(off, table) for table in tables} == before
    assert [delivery.status for delivery in await _deliveries(off)] == statuses_before
    assert off.bot.attempted == []
    assert off.shop.attempted == []


# --------------------------------------------------------------------------- #
# Load                                                                        #
# --------------------------------------------------------------------------- #


async def test_three_hundred_buyers_at_one_minute_within_the_rate(make_worker: Factory) -> None:
    worker = make_worker(send_batch_size=50)
    buyers = range(20_001, 20_301)
    for user_id in buyers:
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 5)
    worker.at(moscow(0, 12))
    started = worker.time.now

    await worker.tick()

    sent = Counter(getattr(call, "chat_id", None) for call in worker.bot.items())
    assert len(sent) == 300
    assert set(sent.values()) == {1}
    # Twenty a second at most: 300 sends take at least 299 / 20 seconds.
    assert worker.time.now - started >= 299 / 20 - 1e-9
    statuses = Counter(delivery.status for delivery in await _deliveries(worker))
    assert statuses == {DailyDeliveryStatus.SENT: 300}


async def test_the_outbox_message_records_say_what_was_sent(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    await _library(worker, 1)
    await worker.run_days(0, 1)

    async with worker.uow_factory() as uow:
        rows = await uow.session.execute(text("SELECT id FROM daily_outbox"))
        messages = [await uow.daily_outbox.get(row[0]) for row in rows]
    (notice,) = messages
    assert notice is not None
    assert notice.status is DailyOutboxStatus.SENT
    assert notice.sent_at is not None
    assert notice.dedupe_key.startswith("exhausted:9501:")


async def test_a_backlog_bigger_than_a_batch_is_drained_in_one_tick(worker: Worker) -> None:
    for user_id in range(9501, 9526):
        await _subscriber(worker, user_id, anchor=12 * 60)
    await _library(worker, 3)
    worker.at(moscow(0, 12))

    await asyncio.wait_for(worker.tick(), timeout=60)

    assert sum(len(_videos_to(worker, user_id)) for user_id in range(9501, 9526)) == 25


# --------------------------------------------------------------------------- #
# Races: two workers deciding the same thing at once                          #
# --------------------------------------------------------------------------- #


async def test_two_evaluators_notify_one_transition_once(worker: Worker) -> None:
    """Both would read the alert as OK and both fire, if nothing queued them.

    The alert already exists — as it does after its first evaluation: a row
    being created would queue the two by its insert alone.
    """
    await DailyAlertService(
        uow_factory=worker.uow_factory,
        settings=worker.settings,
        clock=worker.clock,
    ).observe(DailyAlertKey.CONTENT_EXHAUSTED, AlertObservation(firing=False))
    racing = gated(worker.uow_factory, Gate(2), repository="daily_alerts", method="lock")
    alerts = DailyAlertService(uow_factory=racing, settings=worker.settings, clock=worker.clock)
    firing = AlertObservation(firing=True, value={"exhausted_users": 2})

    notices = await all_at_once(
        alerts.observe(DailyAlertKey.CONTENT_EXHAUSTED, firing),
        alerts.observe(DailyAlertKey.CONTENT_EXHAUSTED, firing),
    )

    assert set(notices) == {AlertNotice.FIRE, None}  # one fired, one found it firing
    assert await _count(worker, "daily_outbox") == 1


async def test_two_workers_tell_an_exhausted_buyer_once(worker: Worker) -> None:
    await _subscriber(worker, 9501, anchor=12 * 60)
    await _library(worker, 1)
    worker.at(moscow(0, 12))
    await worker.tick()  # the only item
    worker.at(moscow(1, 12))
    racing = gated(worker.uow_factory, Gate(2), repository="daily_subscriptions", method="lock")
    notices = DailyNoticeService(uow_factory=racing, settings=worker.settings, clock=worker.clock)

    told = await all_at_once(notices.exhausted(9501), notices.exhausted(9501))

    assert sorted(told) == [False, True]
    assert await _count(worker, "daily_outbox") == 1


# --------------------------------------------------------------------------- #
# The admin panel's messages: answers and "send it to me"                     #
# --------------------------------------------------------------------------- #


def _admin(worker: Worker) -> DailyAdminService:
    return DailyAdminService(
        uow_factory=worker.uow_factory,
        settings=worker.settings,
        library=worker.library(),
        clock=worker.clock,
    )


async def _request(worker: Worker, user_id: int) -> int:
    await seed_buyer(worker.uow_factory, user_id, completed=20)
    async with worker.uow_factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=worker.clock.now)
        await uow.daily_subscriptions.activate(user_id, at=worker.clock.now, anchor_minute=720)
        request, _ = await uow.daily_requests.create(
            DailyRequestDraft(
                user_id=user_id,
                client_draft_id=uuid4(),
                purchases_at_submit=20,
                items=(
                    DailyContentDraft(
                        kind=DailyContentKind.TEXT,
                        source_chat_id=user_id,
                        source_message_id=1,
                        added_by=user_id,
                        body="more of this",
                    ),
                ),
            ),
            at=worker.clock.now,
        )
    return request.id


async def _reply_events(worker: Worker, request_id: int) -> list[DailyRequestEventKind]:
    async with worker.uow_factory() as uow:
        events = await uow.daily_requests.events_of(request_id)
    return [
        event.kind
        for event in events
        if event.kind in (DailyRequestEventKind.REPLY_SENT, DailyRequestEventKind.REPLY_FAILED)
    ]


async def test_an_answer_to_a_request_goes_out_through_the_daily_bot(worker: Worker) -> None:
    request_id = await _request(worker, 9511)
    await _admin(worker).reply("alice", request_id, text="Added it!", client_key=uuid4())

    await worker.tick()
    await worker.tick()

    assert worker.bot.texts_to(9511) == [
        f"💬 Ответ на ваше пожелание #{request_id}:\n\nAdded it!",
    ]
    assert await _reply_events(worker, request_id) == [DailyRequestEventKind.REPLY_SENT]
    assert worker.shop.calls == []


async def test_an_answer_to_a_buyer_who_blocked_the_bot_is_recorded_as_failed(
    worker: Worker,
) -> None:
    request_id = await _request(worker, 9512)
    await _admin(worker).reply("alice", request_id, text="Added it!", client_key=uuid4())
    worker.bot.failures[SendMessage] = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=9512, text="x"),
            message="Forbidden: bot was blocked by the user",
        )
    ]

    await worker.tick()
    await worker.tick()

    assert worker.bot.texts_to(9512) == []
    assert await _reply_events(worker, request_id) == [DailyRequestEventKind.REPLY_FAILED]


async def test_send_to_me_reaches_the_owner_through_the_shared_budget(worker: Worker) -> None:
    (item_id,) = await _library(worker, 1)

    await _admin(worker).send_library_item("alice", item_id, uuid4())
    await worker.tick()

    (sent,) = worker.bot.items_to(OWNER)
    assert isinstance(sent, SendVideo)
    assert sent.protect_content is worker.settings.protect_content
    async with worker.uow_factory() as uow:
        statuses = list(await uow.session.scalars(text("SELECT status FROM daily_outbox")))
    assert statuses == [DailyOutboxStatus.SENT.value]
