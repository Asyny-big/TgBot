"""The Daily runtime services on the in-memory fakes: their own decisions.

The fakes keep the database's rules (see ``test_repository_daily_runtime``);
the worker's path over PostgreSQL, Redis and the bots is in
``test_daily_worker_flow``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import DailySettings
from app.core.exceptions import (
    DailySendBlockedError,
    DailySendError,
    DailySendTransientError,
)
from app.daily_bot import texts
from app.domain.commands import DailyContentDraft, DailyDeliveryDraft, DailyOutboxDraft
from app.domain.daily import activation_window
from app.domain.daily_alerts import AlertNotice, AlertObservation
from app.domain.enums import (
    DailyAlertKey,
    DailyAlertState,
    DailyContentKind,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
)
from app.services.daily_alerts import DailyAlertService
from app.services.daily_delivery import DailyDeliveryService
from app.services.daily_notices import DailyNoticeService
from app.services.daily_outbox import DailyOutboxService, OutboxStep
from app.services.daily_planning import DailyPlanningService
from app.services.daily_stock import DailyStockService
from tests.daily_bot_harness import seed_buyer
from tests.fakes import FakeUnitOfWorkFactory

BUYER = 9401
OWNER = 9400
SECOND_OWNER = 9399
# 12:00 in Moscow. Anchors are minutes of the Moscow day.
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
NOON = 12 * 60


class _NothingSent:
    """Opening a delivery sends nothing."""

    async def send(self, chat_id: int, content: object, *, protect_content: bool) -> int:
        raise AssertionError((chat_id, content, protect_content))


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def _settings(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "admin_telegram_ids": (OWNER,),
        "recovery_hours": 12,
        "max_attempts": 3,
        "retry_initial_seconds": 30,
        "retry_max_seconds": 900,
        "low_stock_days": 3,
        "alert_cooldown_hours": 24,
    }
    values.update(overrides)
    return DailySettings(**values)


async def _subscriber(
    store: FakeUnitOfWorkFactory,
    user_id: int = BUYER,
    *,
    anchor: int = NOON,
    completed: int = 15,
) -> None:
    await seed_buyer(store, user_id, completed=completed)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=anchor)


async def _items(store: FakeUnitOfWorkFactory, count: int) -> list[int]:
    ids = []
    async with store() as uow:
        for _ in range(count):
            item, _ = await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=OWNER,
                    source_message_id=int(uuid4().int % 10_000_000),
                    added_by=OWNER,
                    file_id=f"file-{uuid4().hex}",
                    file_unique_id=f"unique-{uuid4().hex}",
                )
            )
            ids.append(item.id)
    return ids


def _deliveries(store: FakeUnitOfWorkFactory) -> list[Any]:
    return sorted(store.unit.daily_deliveries.items.values(), key=lambda item: item.id)


def _outbox(store: FakeUnitOfWorkFactory) -> list[Any]:
    return sorted(store.unit.daily_outbox.items.values(), key=lambda item: item.id)


# --------------------------------------------------------------------------- #
# The planner                                                                 #
# --------------------------------------------------------------------------- #


def _planner(
    store: FakeUnitOfWorkFactory,
    clock: Clock,
    **settings: Any,
) -> DailyPlanningService:
    page_size = settings.pop("page_size", 200)
    return DailyPlanningService(
        uow_factory=store,
        settings=_settings(**settings),
        clock=clock,
        page_size=page_size,
    )


@pytest.mark.parametrize(
    ("moscow", "opened", "first_attempt"),
    [
        ((10, 59), False, None),  # the window opens at 11:00
        ((11, 0), True, (12, 0)),  # open: the first attempt waits for the anchor
        ((12, 30), True, (12, 30)),  # past the anchor: at once
        ((23, 59), True, (23, 59)),  # late, before the deadline: recovered at once
    ],
)
async def test_a_delivery_opens_with_the_window_and_is_due_at_the_anchor(
    moscow: tuple[int, int],
    opened: bool,
    first_attempt: tuple[int, int] | None,
) -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    clock.now = datetime(2026, 10, 11, moscow[0] - 3, moscow[1], tzinfo=UTC)

    report = await _planner(store, clock).plan()

    assert report.scanned == 1
    assert report.opened == int(opened)
    if first_attempt is not None:
        (delivery,) = _deliveries(store)
        expected = datetime(2026, 10, 11, first_attempt[0] - 3, first_attempt[1], tzinfo=UTC)
        assert delivery.next_attempt_at == expected
        assert delivery.delivery_date == date(2026, 10, 11)


async def test_a_missed_day_is_never_caught_up() -> None:
    """Planner down from yesterday morning: only today's delivery opens."""
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, anchor=9 * 60)
    clock.now = datetime(2026, 10, 12, 7, 30, tzinfo=UTC)  # 10:30 in Moscow, a day later

    await _planner(store, clock).plan()
    await _planner(store, clock).plan()

    (today,) = _deliveries(store)
    assert today.delivery_date == date(2026, 10, 12)


async def test_windows_around_midnight_belong_to_their_own_day() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, BUYER, anchor=23 * 60 + 50)
    await _subscriber(store, BUYER + 1, anchor=30)
    clock.now = datetime(2026, 10, 11, 20, 45, tzinfo=UTC)  # 23:45 in Moscow

    await _planner(store, clock).plan()

    by_user = {delivery.user_id: delivery for delivery in _deliveries(store)}
    assert by_user[BUYER].delivery_date == date(2026, 10, 11)
    assert by_user[BUYER + 1].delivery_date == date(2026, 10, 12)  # 00:30 tomorrow


async def test_only_buyers_who_receive_are_planned() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, BUYER)
    await _subscriber(store, BUYER + 1)
    await _subscriber(store, BUYER + 2)
    await _subscriber(store, BUYER + 3, completed=14)
    await seed_buyer(store, BUYER + 4)  # never activated
    async with store() as uow:
        await uow.daily_subscriptions.pause(BUYER + 1, at=NOW)
        await uow.daily_subscriptions.mark_blocked(BUYER + 2, at=NOW)

    report = await _planner(store, clock).plan()

    assert report.scanned == 1
    assert [delivery.user_id for delivery in _deliveries(store)] == [BUYER]


async def test_a_beta_plans_only_its_buyers() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, BUYER)
    await _subscriber(store, BUYER + 1)

    await _planner(store, clock, beta_user_ids=(BUYER + 1,)).plan()

    assert [delivery.user_id for delivery in _deliveries(store)] == [BUYER + 1]


async def test_planning_pages_through_every_buyer_and_opens_each_once() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    for offset in range(5):
        await _subscriber(store, BUYER + offset)

    first = await _planner(store, clock, page_size=2).plan()
    second = await _planner(store, clock, page_size=2).plan()

    assert (first.scanned, first.opened) == (5, 5)
    assert (second.scanned, second.opened) == (5, 0)
    assert len(_deliveries(store)) == 5


async def test_the_planner_opens_exactly_what_the_engine_would() -> None:
    planned_store, engine_store, clock = FakeUnitOfWorkFactory(), FakeUnitOfWorkFactory(), Clock()
    for store in (planned_store, engine_store):
        await _subscriber(store)
    clock.now = datetime(2026, 10, 11, 8, 20, tzinfo=UTC)  # 11:20 in Moscow
    engine = DailyDeliveryService(
        uow_factory=engine_store,
        sender=_NothingSent(),
        settings=_settings(),
        clock=clock,
    )
    async with engine_store() as uow:
        subscription = await uow.daily_subscriptions.get(BUYER)
    assert subscription is not None

    await _planner(planned_store, clock).plan()
    by_engine = await engine.open_due(subscription, now=clock.now)

    (by_planner,) = _deliveries(planned_store)
    assert by_engine is not None
    fields = (
        "user_id",
        "delivery_date",
        "trigger",
        "status",
        "scheduled_for",
        "window_opens_at",
        "window_closes_at",
        "deadline_at",
        "next_attempt_at",
    )
    assert {name: getattr(by_planner, name) for name in fields} == {
        name: getattr(by_engine, name) for name in fields
    }


async def test_nothing_is_planned_while_daily_content_is_off() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)

    report = await _planner(store, clock, enabled=False).plan()

    assert (report.scanned, report.opened) == (0, 0)
    assert _deliveries(store) == []


# --------------------------------------------------------------------------- #
# The outbox                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class RecordingTextSender:
    sent: list[tuple[int, str]] = field(default_factory=list)
    failures: list[DailySendError] = field(default_factory=list)
    on_send: Any = None

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.on_send is not None:
            self.on_send()
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text))
        return 100 + len(self.sent)


async def _queue(
    store: FakeUnitOfWorkFactory,
    key: str,
    *,
    channel: DailyOutboxChannel = DailyOutboxChannel.DAILY,
    chat_id: int = OWNER,
) -> None:
    async with store() as uow:
        await uow.daily_outbox.enqueue(
            DailyOutboxDraft(
                kind=DailyOutboxKind.ALERT,
                channel=channel,
                chat_id=chat_id,
                body=f"message {key}",
                dedupe_key=key,
                first_attempt_at=NOW,
                deadline_at=NOW + timedelta(hours=6),
            )
        )


def _outbox_service(
    store: FakeUnitOfWorkFactory,
    clock: Clock,
    senders: dict[DailyOutboxChannel, Any],
    **settings: Any,
) -> DailyOutboxService:
    return DailyOutboxService(
        uow_factory=store,
        settings=_settings(**settings),
        senders=senders,
        clock=clock,
        jitter=lambda: 0.0,
    )


async def test_each_message_goes_through_the_bot_of_its_channel() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    daily, shop = RecordingTextSender(), RecordingTextSender()
    await _queue(store, "a")
    await _queue(store, "b", channel=DailyOutboxChannel.SHOP)

    reports = await _outbox_service(
        store,
        clock,
        {DailyOutboxChannel.DAILY: daily, DailyOutboxChannel.SHOP: shop},
    ).process_due()

    assert [report.step for report in reports] == [OutboxStep.SENT, OutboxStep.SENT]
    assert daily.sent == [(OWNER, "message a")]
    assert shop.sent == [(OWNER, "message b")]
    assert {message.status for message in _outbox(store)} == {DailyOutboxStatus.SENT}


async def test_an_uncertain_failure_is_retried_later_then_given_up() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    flaky = RecordingTextSender(
        failures=[
            DailySendTransientError("Telegram flood control", retry_after=120),
            DailySendTransientError("Telegram is unavailable"),
            DailySendTransientError("Telegram is unavailable"),
        ]
    )
    service = _outbox_service(store, clock, {DailyOutboxChannel.DAILY: flaky})
    await _queue(store, "a")

    (first,) = await service.process_due()
    (message,) = _outbox(store)
    assert first.step is OutboxStep.RETRY_SCHEDULED
    assert first.retry_after == 120
    assert message.status is DailyOutboxStatus.PENDING
    assert message.next_attempt_at == NOW + timedelta(seconds=120)  # Telegram's hint wins

    assert await service.process_due() == ()  # not due yet
    clock.now = message.next_attempt_at
    (second,) = await service.process_due()
    assert second.step is OutboxStep.RETRY_SCHEDULED
    clock.now = _outbox(store)[0].next_attempt_at
    (third,) = await service.process_due()

    assert third.step is OutboxStep.FAILED  # max_attempts=3
    assert _outbox(store)[0].status is DailyOutboxStatus.FAILED
    assert flaky.sent == []


async def test_a_definite_refusal_is_not_retried() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    blocked = RecordingTextSender(failures=[DailySendBlockedError("Forbidden: bot was blocked")])
    await _queue(store, "a")

    (report,) = await _outbox_service(
        store, clock, {DailyOutboxChannel.DAILY: blocked}
    ).process_due()

    assert report.step is OutboxStep.FAILED
    (message,) = _outbox(store)
    assert message.status is DailyOutboxStatus.FAILED
    assert message.attempts == 1


async def test_a_message_whose_lease_ran_out_is_handed_back_unsent() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()

    def slow() -> None:
        clock.now += timedelta(minutes=10)  # past the five-minute lease

    sender = RecordingTextSender(on_send=slow)
    await _queue(store, "a")
    await _queue(store, "b")

    reports = await _outbox_service(
        store,
        clock,
        {DailyOutboxChannel.DAILY: sender},
        lease_seconds=300,
    ).process_due()

    assert [report.step for report in reports] == [OutboxStep.SENT, OutboxStep.RETRY_SCHEDULED]
    assert len(sender.sent) == 1


async def test_a_channel_without_a_bot_fails_the_message() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _queue(store, "a", channel=DailyOutboxChannel.SHOP)

    (report,) = await _outbox_service(
        store,
        clock,
        {DailyOutboxChannel.DAILY: RecordingTextSender()},
    ).process_due()

    assert report.step is OutboxStep.FAILED


async def test_nothing_is_sent_or_expired_while_daily_content_is_off() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    sender = RecordingTextSender()
    await _queue(store, "a")
    service = _outbox_service(store, clock, {DailyOutboxChannel.DAILY: sender}, enabled=False)
    clock.now = NOW + timedelta(days=1)

    assert await service.process_due() == ()
    assert await service.expire_overdue() == 0
    assert _outbox(store)[0].status is DailyOutboxStatus.PENDING
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# The alerts                                                                  #
# --------------------------------------------------------------------------- #


def _alerts(store: FakeUnitOfWorkFactory, clock: Clock, **settings: Any) -> DailyAlertService:
    return DailyAlertService(uow_factory=store, settings=_settings(**settings), clock=clock)


FIRING_VALUE: dict[str, object] = {
    "forecast_days": 2.0,
    "threshold_days": 3.0,
    "active_items": 4,
}


async def test_an_alert_fires_reminds_and_resolves_once_each() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    alerts = _alerts(store, clock, admin_telegram_ids=(OWNER, SECOND_OWNER))
    firing = AlertObservation(firing=True, value=FIRING_VALUE)

    fired = await alerts.observe(DailyAlertKey.LOW_STOCK, firing)
    clock.now += timedelta(hours=1)
    quiet = await alerts.observe(DailyAlertKey.LOW_STOCK, firing)
    clock.now = NOW + timedelta(hours=24)
    reminded = await alerts.observe(DailyAlertKey.LOW_STOCK, firing)
    clock.now += timedelta(minutes=10)
    resolved = await alerts.observe(DailyAlertKey.LOW_STOCK, AlertObservation(firing=False))
    after = await alerts.observe(DailyAlertKey.LOW_STOCK, AlertObservation(firing=False))

    assert (fired, quiet, reminded, resolved, after) == (
        AlertNotice.FIRE,
        None,
        AlertNotice.REMIND,
        AlertNotice.RESOLVE,
        None,
    )
    messages = _outbox(store)
    assert [(message.chat_id, message.dedupe_key) for message in messages] == [
        (OWNER, f"alert:low_stock:1:{OWNER}"),
        (SECOND_OWNER, f"alert:low_stock:1:{SECOND_OWNER}"),
        (OWNER, f"alert:low_stock:2:{OWNER}"),
        (SECOND_OWNER, f"alert:low_stock:2:{SECOND_OWNER}"),
        (OWNER, f"alert:low_stock:3:{OWNER}"),
        (SECOND_OWNER, f"alert:low_stock:3:{SECOND_OWNER}"),
    ]
    assert messages[0].body == texts.alert(DailyAlertKey.LOW_STOCK, AlertNotice.FIRE, FIRING_VALUE)
    assert messages[2].body.startswith("🔁")
    assert {message.channel for message in messages} == {DailyOutboxChannel.DAILY}
    assert {message.kind for message in messages} == {DailyOutboxKind.ALERT}
    alert = store.unit.daily_alerts.items[DailyAlertKey.LOW_STOCK]
    assert (alert.state, alert.notifications) == (DailyAlertState.OK, 3)
    assert alert.fired_at == NOW
    assert alert.resolved_at == clock.now


async def test_a_revoked_token_is_reported_through_the_shop_bot() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()

    await _alerts(store, clock).observe(
        DailyAlertKey.DAILY_TOKEN_UNAUTHORIZED,
        AlertObservation(firing=True, value={"token": "unauthorized"}),
    )

    (message,) = _outbox(store)
    assert message.channel is DailyOutboxChannel.SHOP
    assert "401" in message.body


async def test_an_alert_without_owners_is_tracked_but_sends_nothing() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()

    notice = await _alerts(store, clock, admin_telegram_ids=()).observe(
        DailyAlertKey.CONTENT_EXHAUSTED,
        AlertObservation(firing=True),
    )

    assert notice is AlertNotice.FIRE
    assert _outbox(store) == []
    assert store.unit.daily_alerts.items[DailyAlertKey.CONTENT_EXHAUSTED].state is (
        DailyAlertState.FIRING
    )


async def test_alerts_are_not_evaluated_while_daily_content_is_off() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()

    notice = await _alerts(store, clock, enabled=False).observe(
        DailyAlertKey.LOW_STOCK,
        AlertObservation(firing=True, value=FIRING_VALUE),
    )

    assert notice is None
    assert store.unit.daily_alerts.items == {}
    assert _outbox(store) == []


# --------------------------------------------------------------------------- #
# "You have seen everything"                                                  #
# --------------------------------------------------------------------------- #


def _notices(store: FakeUnitOfWorkFactory, clock: Clock, **settings: Any) -> DailyNoticeService:
    return DailyNoticeService(uow_factory=store, settings=_settings(**settings), clock=clock)


async def _received(store: FakeUnitOfWorkFactory, user_id: int, at: datetime) -> None:
    """One item sent to the buyer at ``at``."""
    (content_id,) = await _items(store, 1)
    deliveries = store.unit.daily_deliveries
    delivery = await deliveries.open(
        DailyDeliveryDraft(
            user_id=user_id,
            trigger=DailyDeliveryTrigger.SCHEDULE,
            window=activation_window(at, recovery=timedelta(hours=12)),
            first_attempt_at=at,
        )
    )
    assert delivery is not None
    attempt = await deliveries.start_attempt(
        delivery.id,
        content_id=content_id,
        now=at,
        lease_until=at + timedelta(minutes=5),
    )
    await deliveries.finish_sent(attempt.lease_token, at=at, telegram_message_id=1)


async def test_the_exhausted_notice_goes_once_per_episode() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    notices = _notices(store, clock)
    await _received(store, BUYER, NOW - timedelta(days=1))

    first = await notices.exhausted(BUYER)
    clock.now += timedelta(minutes=15)  # the same day looked at again
    again = await notices.exhausted(BUYER)
    told_at = store.unit.daily_subscriptions.items[BUYER].exhausted_notified_at
    assert told_at == NOW  # the episode was recorded once, not refreshed
    await _received(store, BUYER, NOW + timedelta(days=1))  # a new item reached them
    clock.now = NOW + timedelta(days=2)
    second_episode = await notices.exhausted(BUYER)

    assert (first, again, second_episode) == (True, False, True)
    messages = _outbox(store)
    assert [message.body for message in messages] == [texts.EXHAUSTED, texts.EXHAUSTED]
    assert [message.kind for message in messages] == [DailyOutboxKind.EXHAUSTED_NOTICE] * 2
    assert {message.chat_id for message in messages} == {BUYER}
    assert len({message.dedupe_key for message in messages}) == 2


async def test_nobody_is_told_they_saw_everything_before_they_saw_anything() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)

    assert await _notices(store, clock).exhausted(BUYER) is False
    assert _outbox(store) == []


async def test_a_paused_buyer_or_daily_content_off_hears_nothing() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, BUYER)
    await _subscriber(store, BUYER + 1)
    await _received(store, BUYER, NOW - timedelta(days=1))
    await _received(store, BUYER + 1, NOW - timedelta(days=1))
    async with store() as uow:
        await uow.daily_subscriptions.pause(BUYER, at=NOW)

    assert await _notices(store, clock).exhausted(BUYER) is False
    assert await _notices(store, clock, enabled=False).exhausted(BUYER + 1) is False
    assert _outbox(store) == []


# --------------------------------------------------------------------------- #
# The stock                                                                   #
# --------------------------------------------------------------------------- #


async def test_the_stock_is_counted_per_buyer() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, BUYER)
    await _subscriber(store, BUYER + 1)
    await _items(store, 6)
    await _received(store, BUYER, NOW - timedelta(days=1))  # a seventh item, received

    snapshot = await DailyStockService(
        uow_factory=store,
        settings=_settings(low_stock_days=3),
        clock=clock,
    ).snapshot()

    assert snapshot.active_items == 7
    assert snapshot.add_rate_per_day == pytest.approx(1.0)  # seven in the last week
    summary = snapshot.summary
    assert summary is not None
    assert summary.active_users == 2
    assert summary.minimum_remaining == 6
    assert summary.average_remaining == pytest.approx(6.5)
    assert summary.exhausted_users == 0


async def test_without_buyers_there_is_no_stock_summary() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _items(store, 3)

    snapshot = await DailyStockService(
        uow_factory=store, settings=_settings(), clock=clock
    ).snapshot()

    assert snapshot.summary is None
    assert snapshot.active_items == 3


async def test_recent_outcomes_look_at_the_last_day() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    await _received(store, BUYER, NOW - timedelta(hours=2))
    await _received(store, BUYER, NOW - timedelta(days=3))

    counts = await DailyStockService(
        uow_factory=store, settings=_settings(), clock=clock
    ).recent_outcomes()

    assert (counts.sent, counts.failed) == (1, 0)
    assert {delivery.status for delivery in _deliveries(store)} == {DailyDeliveryStatus.SENT}
