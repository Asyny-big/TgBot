"""Daily runtime repositories: one contract, two implementations.

The first part runs against the fakes *and* PostgreSQL — the fakes the
service tests use cannot drift from what the database does. The second part is
PostgreSQL only: two transactions at once, made to collide on purpose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from app.domain.commands import DailyContentDraft, DailyDeliveryDraft, DailyOutboxDraft
from app.domain.daily import DeliveryCounts, activation_window, window_for
from app.domain.enums import (
    DailyAlertKey,
    DailyAlertState,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
)
from tests.concurrency import Gate, all_at_once, gated
from tests.daily_bot_harness import seed_buyer
from tests.fakes import FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import DailyContent, DailyDelivery, DailyOutboxMessage
    from app.domain.uow import UnitOfWorkFactory
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9301
OTHER = 9302
OWNER = 9300
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
NOON = 12 * 60
RECOVERY = timedelta(hours=12)
LEASE = timedelta(minutes=5)
TODAY = date(2026, 10, 11)


@pytest.fixture(params=["fake", "postgres"])
def store(request: pytest.FixtureRequest) -> UnitOfWorkFactory:
    """The fakes, or PostgreSQL with real commits."""
    if request.param == "fake":
        return FakeUnitOfWorkFactory()
    factory: SqlAlchemyUnitOfWorkFactory = request.getfixturevalue("live_uow_factory")
    return factory


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


async def _subscriber(
    store: UnitOfWorkFactory,
    user_id: int = BUYER,
    *,
    completed: int = 15,
    anchor: int = NOON,
) -> None:
    await seed_buyer(store, user_id, completed=completed)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=anchor)


async def _items(store: UnitOfWorkFactory, count: int) -> list[DailyContent]:
    items = []
    async with store() as uow:
        for _ in range(count):
            number = int(uuid4().int % 10_000_000)
            item, _ = await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=OWNER,
                    source_message_id=number,
                    added_by=OWNER,
                    file_id=f"file-{number}",
                    file_unique_id=f"unique-{uuid4().hex}",
                )
            )
            items.append(item)
    return items


def _scheduled(user_id: int = BUYER, day: date = TODAY) -> DailyDeliveryDraft:
    window = window_for(NOON, day, recovery=RECOVERY)
    return DailyDeliveryDraft(
        user_id=user_id,
        trigger=DailyDeliveryTrigger.SCHEDULE,
        window=window,
        first_attempt_at=window.center,
    )


async def _open(store: UnitOfWorkFactory, draft: DailyDeliveryDraft) -> DailyDelivery:
    async with store() as uow:
        delivery = await uow.daily_deliveries.open(draft)
    assert delivery is not None
    return delivery


async def _send(store: UnitOfWorkFactory, delivery: DailyDelivery, content_id: int) -> None:
    """Start an attempt and record it sent."""
    async with store() as uow:
        attempt = await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=content_id,
            now=NOW,
            lease_until=NOW + LEASE,
        )
        await uow.daily_deliveries.finish_sent(attempt.lease_token, at=NOW, telegram_message_id=1)


async def _fail(
    store: UnitOfWorkFactory,
    delivery: DailyDelivery,
    content_id: int,
    outcome: DailyAttemptOutcome,
) -> None:
    async with store() as uow:
        attempt = await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=content_id,
            now=NOW,
            lease_until=NOW + LEASE,
        )
        await uow.daily_deliveries.finish_failed(
            attempt.lease_token,
            at=NOW,
            outcome=outcome,
            error="refused",
        )


def _message(
    key: str = "notice:1",
    *,
    chat_id: int = BUYER,
    at: datetime = NOW,
    deadline: timedelta = timedelta(hours=1),
) -> DailyOutboxDraft:
    return DailyOutboxDraft(
        kind=DailyOutboxKind.EXHAUSTED_NOTICE,
        channel=DailyOutboxChannel.DAILY,
        chat_id=chat_id,
        body="Вы посмотрели все материалы",
        dedupe_key=key,
        first_attempt_at=at,
        deadline_at=at + deadline,
    )


def _lease(message: DailyOutboxMessage) -> UUID:
    assert message.lease_token is not None
    return message.lease_token


async def _status(uow: Any, dedupe_key: str) -> DailyOutboxStatus:
    message = await uow.daily_outbox.find_by_key(dedupe_key)
    assert message is not None
    status: DailyOutboxStatus = message.status
    return status


# --------------------------------------------------------------------------- #
# Deliveries: planning in bulk, the activation grace, the counts              #
# --------------------------------------------------------------------------- #


async def test_open_many_opens_each_buyer_once_a_day(store: UnitOfWorkFactory) -> None:
    for user_id in (BUYER, OTHER, OTHER + 1):
        await _subscriber(store, user_id)
    async with store() as uow:
        first = await uow.daily_deliveries.open_many([_scheduled(BUYER), _scheduled(OTHER)])
        again = await uow.daily_deliveries.open_many([_scheduled(BUYER), _scheduled(OTHER)])
        mixed = await uow.daily_deliveries.open_many([_scheduled(BUYER), _scheduled(OTHER + 1)])
        nothing = await uow.daily_deliveries.open_many([])
        opened = await uow.daily_deliveries.for_date(BUYER, TODAY)
    assert (first, again, mixed, nothing) == (2, 0, 1, 0)

    window = window_for(NOON, TODAY, recovery=RECOVERY)
    assert opened is not None
    assert opened.status is DailyDeliveryStatus.PENDING
    assert opened.trigger is DailyDeliveryTrigger.SCHEDULE
    assert (opened.scheduled_for, opened.next_attempt_at, opened.deadline_at) == (
        window.center,
        window.center,
        window.deadline_at,
    )
    assert (opened.window_opens_at, opened.window_closes_at) == (
        window.opens_at,
        window.closes_at,
    )


async def test_a_fresh_activation_is_left_to_the_bot(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    await _subscriber(store, OTHER)
    activation = await _open(
        store,
        DailyDeliveryDraft(
            user_id=BUYER,
            trigger=DailyDeliveryTrigger.ACTIVATION,
            window=activation_window(NOW, recovery=RECOVERY),
            first_attempt_at=NOW,
        ),
    )
    schedule = await _open(
        store,
        DailyDeliveryDraft(
            user_id=OTHER,
            trigger=DailyDeliveryTrigger.SCHEDULE,
            window=activation_window(NOW, recovery=RECOVERY),
            first_attempt_at=NOW,
        ),
    )
    grace = timedelta(seconds=120)

    async def due(at: datetime, *, with_grace: bool = True) -> set[int]:
        async with store() as uow:
            locked = await uow.daily_deliveries.lock_due(
                now=at,
                limit=10,
                activation_grace=grace if with_grace else None,
            )
        return {delivery.id for delivery in locked}

    assert await due(NOW + timedelta(seconds=60)) == {schedule.id}
    assert await due(NOW + timedelta(seconds=60), with_grace=False) == {activation.id, schedule.id}
    assert await due(NOW + timedelta(seconds=121)) == {activation.id, schedule.id}

    # Once the bot found nothing to send, the worker's rechecks are not held back.
    async with store() as uow:
        await uow.daily_deliveries.mark_no_content(activation.id, at=NOW, recheck_at=NOW)
    assert await due(NOW + timedelta(seconds=30)) == {activation.id, schedule.id}


async def test_last_sent_and_the_failure_counts(store: UnitOfWorkFactory) -> None:
    for user_id in range(BUYER, BUYER + 5):
        await _subscriber(store, user_id)
    items = await _items(store, 5)
    deliveries = [await _open(store, _scheduled(user_id)) for user_id in range(BUYER, BUYER + 5)]
    await _send(store, deliveries[0], items[0].id)
    await _fail(store, deliveries[1], items[1].id, DailyAttemptOutcome.PERMANENT)
    await _fail(store, deliveries[2], items[2].id, DailyAttemptOutcome.BLOCKED)
    # Expired without a single attempt — the library was dry: not a failure.
    async with store() as uow:
        await uow.daily_deliveries.expire_overdue(now=deliveries[3].deadline_at, limit=1)

    async with store() as uow:
        last = await uow.daily_deliveries.last_sent_at(BUYER)
        never = await uow.daily_deliveries.last_sent_at(BUYER + 1)
        recent = await uow.daily_deliveries.outcome_counts(since=NOW - timedelta(hours=24))
        later = await uow.daily_deliveries.outcome_counts(since=NOW + timedelta(days=1))
    assert last == NOW
    assert never is None
    assert recent == DeliveryCounts(sent=1, failed=1)
    assert later == DeliveryCounts(sent=0, failed=0)


# --------------------------------------------------------------------------- #
# Subscriptions and content: the stock counts                                 #
# --------------------------------------------------------------------------- #


async def test_received_active_counts_follow_what_each_buyer_holds(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store, BUYER)
    await _subscriber(store, OTHER)
    await _subscriber(store, OTHER + 1, completed=14)  # no access: not counted
    items = await _items(store, 4)
    yesterday = await _open(store, _scheduled(BUYER, TODAY - timedelta(days=1)))
    today = await _open(store, _scheduled(BUYER, TODAY))
    await _send(store, yesterday, items[0].id)
    async with store() as uow:  # held while being sent: counts as taken
        await uow.daily_deliveries.start_attempt(
            today.id,
            content_id=items[1].id,
            now=NOW,
            lease_until=NOW + LEASE,
        )
        await uow.daily_content.set_status(items[0].id, DailyContentStatus.ARCHIVED, at=NOW)

    async with store() as uow:
        counts = await uow.daily_subscriptions.received_active_counts(min_completed=15)
        beta = await uow.daily_subscriptions.received_active_counts(
            min_completed=15,
            only=(OTHER,),
        )
        await uow.daily_subscriptions.pause(OTHER, at=NOW)
        paused = await uow.daily_subscriptions.received_active_counts(min_completed=15)
    # The archived item no longer counts: it is out of everybody's stock.
    assert counts == {BUYER: 1, OTHER: 0}
    assert beta == {OTHER: 0}
    assert paused == {BUYER: 1}


async def test_the_exhausted_notice_is_recorded_on_the_subscription(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    async with store() as uow:
        marked = await uow.daily_subscriptions.mark_exhausted_notified(BUYER, at=NOW)
        missing = await uow.daily_subscriptions.mark_exhausted_notified(404, at=NOW)
    assert marked is not None
    assert marked.exhausted_notified_at == NOW
    assert missing is None


async def test_items_added_since(store: UnitOfWorkFactory) -> None:
    await _items(store, 3)

    async with store() as uow:
        recent = await uow.daily_content.added_since(datetime.now(UTC) - timedelta(minutes=5))
        future = await uow.daily_content.added_since(datetime.now(UTC) + timedelta(minutes=5))
    assert (recent, future) == (3, 0)


# --------------------------------------------------------------------------- #
# The outbox                                                                  #
# --------------------------------------------------------------------------- #


async def test_a_message_is_queued_once_per_key(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        queued = await uow.daily_outbox.enqueue(_message("alert:low_stock:1:9300"))
        again = await uow.daily_outbox.enqueue(_message("alert:low_stock:1:9300"))
        found = await uow.daily_outbox.find_by_key("alert:low_stock:1:9300")
    assert queued is not None
    assert again is None
    assert found == queued
    assert queued.status is DailyOutboxStatus.PENDING
    assert queued.attempts == 0
    assert queued.channel is DailyOutboxChannel.DAILY
    assert queued.kind is DailyOutboxKind.EXHAUSTED_NOTICE


async def test_a_claim_leases_due_messages_and_takes_over_dead_attempts(
    store: UnitOfWorkFactory,
) -> None:
    async with store() as uow:
        due = await uow.daily_outbox.enqueue(_message("due"))
        later = await uow.daily_outbox.enqueue(_message("later", at=NOW + timedelta(hours=1)))
        stale = await uow.daily_outbox.enqueue(_message("stale", deadline=timedelta(minutes=1)))
    assert due is not None
    assert later is not None
    assert stale is not None
    at = NOW + timedelta(minutes=2)

    async with store() as uow:
        claimed = await uow.daily_outbox.claim_due(now=at, limit=10, lease_until=at + LEASE)
    (message,) = claimed
    assert message.id == due.id
    assert message.status is DailyOutboxStatus.SENDING
    assert message.lease_token is not None
    assert message.attempts == 1

    async with store() as uow:  # the live lease keeps it
        assert await uow.daily_outbox.claim_due(now=at, limit=10, lease_until=at + LEASE) == ()

    after = at + LEASE
    async with store() as uow:  # the lease ran out: the attempt died
        (taken_over,) = await uow.daily_outbox.claim_due(
            now=after,
            limit=10,
            lease_until=after + LEASE,
        )
        late = await uow.daily_outbox.finish_sent(
            message.lease_token,
            at=after,
            telegram_message_id=5,
        )
    assert taken_over.id == due.id
    assert taken_over.attempts == 2
    assert taken_over.lease_token != message.lease_token
    assert late is None  # the dead attempt can no longer change it


async def test_outcomes_are_written_against_the_lease(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        for key in ("sent", "retry", "failed"):
            await uow.daily_outbox.enqueue(_message(key))
        claimed = await uow.daily_outbox.claim_due(now=NOW, limit=10, lease_until=NOW + LEASE)
    by_key = {message.dedupe_key: message for message in claimed}

    async with store() as uow:
        sent = await uow.daily_outbox.finish_sent(
            _lease(by_key["sent"]),
            at=NOW,
            telegram_message_id=77,
        )
        retry = await uow.daily_outbox.finish_retry(
            _lease(by_key["retry"]),
            at=NOW,
            next_attempt_at=NOW + timedelta(seconds=30),
            error="Telegram is unavailable",
        )
        failed = await uow.daily_outbox.finish_failed(
            _lease(by_key["failed"]),
            at=NOW,
            error="Forbidden: bot was blocked by the user",
        )
        twice = await uow.daily_outbox.finish_failed(
            _lease(by_key["sent"]),
            at=NOW,
            error="late",
        )
    assert sent is not None
    assert (sent.status, sent.sent_at, sent.telegram_message_id) == (
        DailyOutboxStatus.SENT,
        NOW,
        77,
    )
    assert sent.lease_token is None
    assert retry is not None
    assert (retry.status, retry.next_attempt_at, retry.last_error) == (
        DailyOutboxStatus.PENDING,
        NOW + timedelta(seconds=30),
        "Telegram is unavailable",
    )
    assert retry.lease_token is None
    assert failed is not None
    assert failed.status is DailyOutboxStatus.FAILED
    assert twice is None


async def test_messages_past_their_deadline_expire(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        await uow.daily_outbox.enqueue(_message("pending"))
        await uow.daily_outbox.enqueue(_message("sending"))
        await uow.daily_outbox.enqueue(_message("sent"))
        claimed = await uow.daily_outbox.claim_due(now=NOW, limit=10, lease_until=NOW + LEASE)
    by_key = {message.dedupe_key: message for message in claimed}
    async with store() as uow:
        await uow.daily_outbox.finish_sent(
            _lease(by_key["sent"]),
            at=NOW,
            telegram_message_id=1,
        )
        await uow.daily_outbox.finish_retry(
            _lease(by_key["pending"]),
            at=NOW,
            next_attempt_at=NOW,
            error="later",
        )

    async with store() as uow:
        early = await uow.daily_outbox.expire_overdue(now=NOW + timedelta(minutes=59))
        expired = await uow.daily_outbox.expire_overdue(now=NOW + timedelta(hours=1))
        statuses = {key: await _status(uow, key) for key in ("pending", "sending", "sent")}
    assert (early, expired) == (0, 2)
    assert statuses == {
        "pending": DailyOutboxStatus.EXPIRED,
        "sending": DailyOutboxStatus.EXPIRED,
        "sent": DailyOutboxStatus.SENT,
    }


# --------------------------------------------------------------------------- #
# The alerts                                                                  #
# --------------------------------------------------------------------------- #


async def test_an_alert_starts_ok_and_keeps_what_was_saved(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        fresh = await uow.daily_alerts.lock(DailyAlertKey.LOW_STOCK)
        saved = await uow.daily_alerts.save(
            replace(
                fresh,
                state=DailyAlertState.FIRING,
                notifications=1,
                value={"forecast_days": 2.5, "active_items": 3},
                fired_at=NOW,
                notified_at=NOW,
            )
        )
    async with store() as uow:
        again = await uow.daily_alerts.lock(DailyAlertKey.LOW_STOCK)
        missing = await uow.daily_alerts.get(DailyAlertKey.DELIVERY_FAILURES)
        listed = await uow.daily_alerts.list_all()
    assert fresh.state is DailyAlertState.OK
    assert fresh.notifications == 0
    assert fresh.value == {}
    assert saved.state is DailyAlertState.FIRING
    assert again.notifications == 1
    assert again.value == {"forecast_days": 2.5, "active_items": 3}
    assert again.fired_at == NOW
    assert missing is None
    assert [alert.key for alert in listed] == [DailyAlertKey.LOW_STOCK]


# --------------------------------------------------------------------------- #
# PostgreSQL only: collisions                                                 #
# --------------------------------------------------------------------------- #


async def test_two_planners_at_once_open_each_day_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    for user_id in (BUYER, OTHER):
        await _subscriber(live_uow_factory, user_id)
    racing = gated(live_uow_factory, Gate(2), repository="daily_deliveries", method="open_many")

    async def plan() -> Any:
        async with racing() as uow:
            return await uow.daily_deliveries.open_many([_scheduled(BUYER), _scheduled(OTHER)])

    results = await all_at_once(plan(), plan())

    assert sorted(results) == [0, 2]


async def test_two_claimers_split_the_outbox(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    async with live_uow_factory() as uow:
        for number in range(6):
            await uow.daily_outbox.enqueue(_message(f"split:{number}"))
    racing = gated(live_uow_factory, Gate(2), repository="daily_outbox", method="claim_due")

    async def claim() -> Any:
        async with racing() as uow:
            return await uow.daily_outbox.claim_due(now=NOW, limit=3, lease_until=NOW + LEASE)

    first, second = await all_at_once(claim(), claim())

    mine = {message.id for message in first}
    theirs = {message.id for message in second}
    assert len(mine) == len(theirs) == 3
    assert not mine & theirs
