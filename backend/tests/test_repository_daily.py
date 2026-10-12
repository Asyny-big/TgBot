"""Daily Content repositories: one contract, two implementations.

Every scenario in the first part runs against the in-memory fakes *and* against
PostgreSQL, so the fakes later phases test with cannot drift from what the
database enforces. The second part is PostgreSQL only: concurrency (two
transactions at once) and the schema's own checks.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, DailyContentDuplicateError
from app.domain.commands import (
    DailyContentDraft,
    DailyDeliveryDraft,
    ProductDraft,
    PurchaseDraft,
    UserDraft,
)
from app.domain.daily import DAILY_ACCESS_MILESTONE, window_for
from app.domain.enums import (
    Currency,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailySubscriptionStatus,
    PaymentProvider,
    PurchaseStatus,
)
from tests.concurrency import Gate, all_at_once, gated
from tests.fakes import FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import DailyContent, DailyDelivery
    from app.domain.uow import UnitOfWorkFactory
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9901
OTHER = 9902
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
NOON = 12 * 60
RECOVERY = timedelta(hours=12)
LEASE = timedelta(minutes=5)
DAY_ONE = date(2026, 10, 10)
DAY_TWO = date(2026, 10, 11)


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


async def _buyer(store: UnitOfWorkFactory, user_id: int, *, completed: int = 0) -> list[UUID]:
    """A buyer with ``completed`` delivered packs, one product each; their ids."""
    delivered: list[UUID] = []
    async with store() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for _ in range(completed):
            product = await uow.products.create(
                ProductDraft(
                    slug=f"d{uuid4().hex[:10]}",
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
            delivered.append(paid.id)
    return delivered


async def _subscriber(
    store: UnitOfWorkFactory, user_id: int = BUYER, *, completed: int = 15
) -> None:
    """An activated subscriber anchored at noon, Moscow time."""
    await _buyer(store, user_id, completed=completed)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=NOON)


def _draft(number: int, *, kind: DailyContentKind = DailyContentKind.VIDEO) -> DailyContentDraft:
    if kind is DailyContentKind.TEXT:
        return DailyContentDraft(
            kind=kind,
            source_chat_id=1,
            source_message_id=number,
            added_by=1,
            body=f"Text item {number}",
        )
    return DailyContentDraft(
        kind=kind,
        source_chat_id=1,
        source_message_id=number,
        added_by=1,
        file_id=f"file-{number}",
        file_unique_id=f"unique-{number}",
        file_size=1024,
        duration=30,
        body=f"Caption {number}",
        entities=({"type": "bold", "offset": 0, "length": 7},),
    )


async def _library(store: UnitOfWorkFactory, count: int) -> list[DailyContent]:
    items = []
    async with store() as uow:
        for number in range(1, count + 1):
            item, _ = await uow.daily_content.add(_draft(number))
            items.append(item)
    return items


async def _open(
    store: UnitOfWorkFactory,
    delivery_date: date = DAY_ONE,
    *,
    user_id: int = BUYER,
) -> DailyDelivery:
    window = window_for(NOON, delivery_date, recovery=RECOVERY)
    async with store() as uow:
        delivery = await uow.daily_deliveries.open(
            DailyDeliveryDraft(
                user_id=user_id,
                trigger=DailyDeliveryTrigger.SCHEDULE,
                window=window,
                first_attempt_at=window.center,
            )
        )
    assert delivery is not None
    return delivery


async def _start(
    store: UnitOfWorkFactory,
    delivery: DailyDelivery,
    content_id: int,
    *,
    now: datetime = NOW,
) -> Any:
    async with store() as uow:
        return await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=content_id,
            now=now,
            lease_until=now + LEASE,
        )


async def _get(store: UnitOfWorkFactory, delivery_id: int) -> DailyDelivery:
    async with store() as uow:
        found = await uow.daily_deliveries.get(delivery_id)
    assert found is not None
    return found


async def _pick(store: UnitOfWorkFactory, user_id: int = BUYER) -> DailyContent | None:
    async with store() as uow:
        return await uow.daily_content.pick_for(user_id)


# --------------------------------------------------------------------------- #
# Subscriptions                                                               #
# --------------------------------------------------------------------------- #


async def test_the_first_interaction_is_recorded_once(store: UnitOfWorkFactory) -> None:
    await _buyer(store, BUYER)
    async with store() as uow:
        first = await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
        again = await uow.daily_subscriptions.record_first_interaction(
            BUYER,
            at=NOW + timedelta(hours=3),
        )
    assert first.status is DailySubscriptionStatus.INACTIVE
    assert again.first_interaction_at == NOW
    assert not again.receives_deliveries


async def test_activation_fixes_the_anchor_once(store: UnitOfWorkFactory) -> None:
    await _buyer(store, BUYER)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
        active = await uow.daily_subscriptions.activate(BUYER, at=NOW, anchor_minute=NOON)
    assert active.status is DailySubscriptionStatus.ACTIVE
    assert active.anchor_minute == NOON
    assert active.activated_at == NOW
    assert active.receives_deliveries

    with pytest.raises(ConflictError):
        async with store() as uow:
            await uow.daily_subscriptions.activate(BUYER, at=NOW, anchor_minute=0)
    async with store() as uow:
        kept = await uow.daily_subscriptions.get(BUYER)
    assert kept is not None
    assert kept.anchor_minute == NOON


async def test_pause_resume_and_the_bot_being_blocked(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    later = NOW + timedelta(hours=1)
    async with store() as uow:
        repository = uow.daily_subscriptions
        paused = await repository.pause(BUYER, at=later)
        assert paused.status is DailySubscriptionStatus.PAUSED
        assert (await repository.pause(BUYER, at=later)).paused_at == later  # idempotent
        resumed = await repository.resume(BUYER, at=later)
        assert resumed.status is DailySubscriptionStatus.ACTIVE
        assert resumed.paused_at is None
        assert resumed.anchor_minute == NOON

        blocked = await repository.mark_blocked(BUYER, at=later)
        assert blocked is not None
        assert blocked.is_blocked
        assert not blocked.receives_deliveries
        again = await repository.mark_blocked(BUYER, at=later + timedelta(hours=1))
        assert again is not None
        assert again.blocked_at == later  # the first time is kept
        unblocked = await repository.mark_unblocked(BUYER)
        assert unblocked is not None
        assert unblocked.receives_deliveries

        assert await repository.mark_blocked(OTHER, at=later) is None
        assert await repository.mark_unblocked(OTHER) is None


async def test_an_inactive_subscription_cannot_be_paused_or_resumed(
    store: UnitOfWorkFactory,
) -> None:
    await _buyer(store, BUYER)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
    for change in ("pause", "resume"):
        with pytest.raises(ConflictError):
            async with store() as uow:
                await getattr(uow.daily_subscriptions, change)(BUYER, at=NOW)
    with pytest.raises(ConflictError):
        async with store() as uow:
            await uow.daily_subscriptions.pause(OTHER, at=NOW)


async def test_deliverable_reads_access_off_the_loyalty_count(store: UnitOfWorkFactory) -> None:
    """14 completed is not enough; 15 and 25 are; paused, blocked or inactive is out."""
    plan = {
        101: (DAILY_ACCESS_MILESTONE - 1, "active"),
        102: (DAILY_ACCESS_MILESTONE, "active"),
        103: (25, "active"),
        104: (20, "paused"),
        105: (20, "blocked"),
        106: (20, "inactive"),
    }
    for user_id, (completed, state) in plan.items():
        await _buyer(store, user_id, completed=completed)
        async with store() as uow:
            repository = uow.daily_subscriptions
            await repository.record_first_interaction(user_id, at=NOW)
            if state != "inactive":
                await repository.activate(user_id, at=NOW, anchor_minute=NOON)
            if state == "paused":
                await repository.pause(user_id, at=NOW)
            if state == "blocked":
                await repository.mark_blocked(user_id, at=NOW)

    async with store() as uow:
        repository = uow.daily_subscriptions
        everyone = await repository.deliverable(min_completed=DAILY_ACCESS_MILESTONE)
        first_page = await repository.deliverable(min_completed=DAILY_ACCESS_MILESTONE, limit=1)
        second_page = await repository.deliverable(
            min_completed=DAILY_ACCESS_MILESTONE,
            after_user_id=first_page[0].user_id,
        )
        beta = await repository.deliverable(
            min_completed=DAILY_ACCESS_MILESTONE,
            only=(101, 103),
        )

    assert [item.user_id for item in everyone] == [102, 103]
    assert [item.user_id for item in first_page] == [102]
    assert [item.user_id for item in second_page] == [103]
    assert [item.user_id for item in beta] == [103]


async def test_a_refund_after_delivery_still_counts(store: UnitOfWorkFactory) -> None:
    """The loyalty semantics, inherited as is: a refund does not take access back."""
    delivered = await _buyer(store, BUYER, completed=DAILY_ACCESS_MILESTONE)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
        await uow.daily_subscriptions.activate(BUYER, at=NOW, anchor_minute=NOON)
        refunded = await uow.purchases.mark_refunded(delivered[0])
    assert refunded.status is PurchaseStatus.REFUNDED
    async with store() as uow:
        completed = await uow.loyalty.count_completed(BUYER)
        deliverable = await uow.daily_subscriptions.deliverable(
            min_completed=DAILY_ACCESS_MILESTONE,
        )
    assert completed == DAILY_ACCESS_MILESTONE
    assert [item.user_id for item in deliverable] == [BUYER]


# --------------------------------------------------------------------------- #
# Library                                                                     #
# --------------------------------------------------------------------------- #


async def test_an_item_is_added_once_per_message(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        item, created = await uow.daily_content.add(_draft(1))
        again, created_again = await uow.daily_content.add(_draft(1))
    assert created
    assert not created_again
    assert again.id == item.id
    assert item.status is DailyContentStatus.ACTIVE
    assert item.kind is DailyContentKind.VIDEO
    assert item.file_id == "file-1"
    assert item.body == "Caption 1"
    assert item.entities == ({"type": "bold", "offset": 0, "length": 7},)


async def test_text_items_carry_no_file(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        item, created = await uow.daily_content.add(_draft(5, kind=DailyContentKind.TEXT))
    assert created
    assert item.kind is DailyContentKind.TEXT
    assert item.file_id is None
    assert item.body == "Text item 5"


async def test_the_same_file_from_another_message_is_a_duplicate(
    store: UnitOfWorkFactory,
) -> None:
    async with store() as uow:
        original, _ = await uow.daily_content.add(_draft(1))
    forwarded_again = DailyContentDraft(
        kind=DailyContentKind.VIDEO,
        source_chat_id=1,
        source_message_id=99,
        added_by=1,
        file_id="file-1-new-id",
        file_unique_id="unique-1",
    )
    with pytest.raises(DailyContentDuplicateError) as caught:
        async with store() as uow:
            await uow.daily_content.add(forwarded_again)
    assert caught.value.existing_id == original.id

    # Archived, the file may come in again — and the archived one cannot come back.
    async with store() as uow:
        await uow.daily_content.set_status(original.id, DailyContentStatus.ARCHIVED, at=NOW)
        readded, created = await uow.daily_content.add(forwarded_again)
    assert created
    with pytest.raises(DailyContentDuplicateError) as restored:
        async with store() as uow:
            await uow.daily_content.set_status(original.id, DailyContentStatus.ACTIVE, at=NOW)
    assert restored.value.existing_id == readded.id


async def test_items_are_switched_off_never_deleted(store: UnitOfWorkFactory) -> None:
    items = await _library(store, 3)
    async with store() as uow:
        disabled = await uow.daily_content.set_status(
            items[0].id,
            DailyContentStatus.DISABLED,
            at=NOW,
            reason="owner",
        )
        archived = await uow.daily_content.set_status(
            items[1].id,
            DailyContentStatus.ARCHIVED,
            at=NOW,
        )
        same = await uow.daily_content.set_status(
            items[1].id,
            DailyContentStatus.ARCHIVED,
            at=NOW + timedelta(hours=1),
        )
        active = await uow.daily_content.count_active()
        kept = await uow.daily_content.get(items[1].id)
    assert disabled.status_reason == "owner"
    assert archived.status is DailyContentStatus.ARCHIVED
    assert same.status_changed_at == archived.status_changed_at  # idempotent
    assert active == 1
    assert kept is not None

    with pytest.raises(ConflictError):
        async with store() as uow:
            await uow.daily_content.set_status(10_000, DailyContentStatus.ARCHIVED, at=NOW)


def _text(number: int, body: str = "Мысль дня") -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=1,
        source_message_id=number,
        added_by=1,
        body=body,
    )


async def test_exactly_the_same_text_is_a_duplicate(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        first, _ = await uow.daily_content.add(_text(1))
        replay, created = await uow.daily_content.add(_text(1))
        other, other_created = await uow.daily_content.add(_text(2, body="Мысль дня!"))
    assert replay.id == first.id
    assert not created
    assert other_created
    assert other.id != first.id

    with pytest.raises(DailyContentDuplicateError) as caught:
        async with store() as uow:
            await uow.daily_content.add(_text(3))
    assert caught.value.existing_id == first.id


async def test_an_archived_text_may_come_in_again_but_not_back_beside_its_twin(
    store: UnitOfWorkFactory,
) -> None:
    async with store() as uow:
        first, _ = await uow.daily_content.add(_text(1))
        await uow.daily_content.set_status(first.id, DailyContentStatus.ARCHIVED, at=NOW)
        readded, created = await uow.daily_content.add(_text(2))
    assert created

    with pytest.raises(DailyContentDuplicateError) as caught:
        async with store() as uow:
            await uow.daily_content.set_status(first.id, DailyContentStatus.ACTIVE, at=NOW)
    assert caught.value.existing_id == readded.id

    async with store() as uow:
        await uow.daily_content.set_status(readded.id, DailyContentStatus.ARCHIVED, at=NOW)
        back = await uow.daily_content.set_status(first.id, DailyContentStatus.ACTIVE, at=NOW)
    assert back.status is DailyContentStatus.ACTIVE


async def test_items_are_found_by_file_and_by_text_live_first(store: UnitOfWorkFactory) -> None:
    async with store() as uow:
        assert await uow.daily_content.find_by_file("unique-1") is None
        assert await uow.daily_content.find_by_text("Мысль дня") is None
        video, _ = await uow.daily_content.add(_draft(1))
        text, _ = await uow.daily_content.add(_text(2))
        await uow.daily_content.set_status(video.id, DailyContentStatus.ARCHIVED, at=NOW)
        await uow.daily_content.set_status(text.id, DailyContentStatus.ARCHIVED, at=NOW)
        archived_video = await uow.daily_content.find_by_file("unique-1")
        archived_text = await uow.daily_content.find_by_text("Мысль дня")
        live_video, _ = await uow.daily_content.add(replace(_draft(3), file_unique_id="unique-1"))
        live_text, _ = await uow.daily_content.add(_text(4))
        found_video = await uow.daily_content.find_by_file("unique-1")
        found_text = await uow.daily_content.find_by_text("Мысль дня")
        caption_is_not_text = await uow.daily_content.find_by_text("Caption 1")
    assert archived_video is not None
    assert archived_video.id == video.id
    assert archived_text is not None
    assert archived_text.id == text.id
    assert found_video == live_video
    assert found_text == live_text
    assert caption_is_not_text is None


async def test_stats_count_every_item_by_status(store: UnitOfWorkFactory) -> None:
    items = await _library(store, 4)
    async with store() as uow:
        await uow.daily_content.add(_text(10))
        await uow.daily_content.set_status(items[0].id, DailyContentStatus.DISABLED, at=NOW)
        await uow.daily_content.set_status(items[1].id, DailyContentStatus.ARCHIVED, at=NOW)
        stats = await uow.daily_content.stats()
        locked = await uow.daily_content.lock(items[2].id)
        missing = await uow.daily_content.lock(10_000)
    assert (stats.active, stats.disabled, stats.archived) == (3, 1, 1)
    assert stats.active_by_kind == {DailyContentKind.VIDEO: 2, DailyContentKind.TEXT: 1}
    assert locked == items[2]
    assert missing is None


async def test_only_active_items_nobody_holds_are_picked(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    items = await _library(store, 3)
    async with store() as uow:
        await uow.daily_content.set_status(items[1].id, DailyContentStatus.DISABLED, at=NOW)
        await uow.daily_content.set_status(items[2].id, DailyContentStatus.ARCHIVED, at=NOW)

    picked = await _pick(store)
    assert picked is not None
    assert picked.id == items[0].id

    delivery = await _open(store)
    await _start(store, delivery, items[0].id)
    assert await _pick(store) is None  # held by an open delivery
    assert await _pick(store, OTHER) is not None  # the library is shared


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #


async def test_one_delivery_per_buyer_and_date(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    first = await _open(store)
    window = window_for(NOON, DAY_ONE, recovery=RECOVERY)
    async with store() as uow:
        second = await uow.daily_deliveries.open(
            DailyDeliveryDraft(
                user_id=BUYER,
                trigger=DailyDeliveryTrigger.ACTIVATION,
                window=window,
                first_attempt_at=NOW,
            )
        )
        found = await uow.daily_deliveries.for_date(BUYER, DAY_ONE)
    assert second is None
    assert found is not None
    assert found.id == first.id
    assert first.status is DailyDeliveryStatus.PENDING
    assert first.window_opens_at == window.opens_at
    assert first.deadline_at == window.deadline_at

    tomorrow = await _open(store, DAY_TWO)
    assert tomorrow.id != first.id


async def test_due_deliveries_are_the_ones_whose_time_came(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    today = await _open(store)
    tomorrow = await _open(store, DAY_TWO)
    async with store() as uow:
        early = await uow.daily_deliveries.lock_due(now=today.window_opens_at, limit=10)
        due = await uow.daily_deliveries.lock_due(now=NOW, limit=10)
        too_late = await uow.daily_deliveries.lock_due(now=today.deadline_at, limit=10)
    assert early == ()
    assert [item.id for item in due] == [today.id]
    assert tomorrow.id not in [item.id for item in too_late]
    assert today.id not in [item.id for item in too_late]


async def test_a_sent_delivery_is_final(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    delivery = await _open(store)

    attempt = await _start(store, delivery, item.id)
    with pytest.raises(ConflictError):  # the lease is live: nobody else may send
        await _start(store, delivery, item.id, now=NOW + timedelta(minutes=1))
    async with store() as uow:
        sent = await uow.daily_deliveries.finish_sent(
            attempt.lease_token,
            at=NOW + timedelta(seconds=2),
            telegram_message_id=777,
        )
        # The same report again changes nothing.
        replay = await uow.daily_deliveries.finish_sent(
            attempt.lease_token,
            at=NOW + timedelta(seconds=3),
            telegram_message_id=778,
        )
        attempts = await uow.daily_deliveries.attempts_of(delivery.id)
        due_later = await uow.daily_deliveries.lock_due(now=NOW + timedelta(hours=2), limit=10)

    assert sent is not None
    assert sent.status is DailyDeliveryStatus.SENT
    assert sent.telegram_message_id == 777
    assert sent.lease_token is None
    assert replay is None
    assert [(a.attempt_no, a.outcome, a.telegram_message_id) for a in attempts] == [
        (1, DailyAttemptOutcome.SENT, 777)
    ]
    assert due_later == ()
    with pytest.raises(ConflictError):
        await _start(store, delivery, item.id, now=NOW + timedelta(hours=1))


async def test_a_retry_sends_the_same_item_again(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    first_item, second_item = await _library(store, 2)
    delivery = await _open(store)
    attempt = await _start(store, delivery, first_item.id)
    retry_at = NOW + timedelta(minutes=1)
    async with store() as uow:
        waiting = await uow.daily_deliveries.finish_retry(
            attempt.lease_token,
            at=NOW + timedelta(seconds=5),
            outcome=DailyAttemptOutcome.TRANSIENT,
            error="Telegram is unavailable",
            next_attempt_at=retry_at,
            retry_after_seconds=60,
        )
    assert waiting is not None
    assert waiting.status is DailyDeliveryStatus.PENDING
    assert waiting.content_id == first_item.id
    assert waiting.last_outcome is DailyAttemptOutcome.TRANSIENT
    picked = await _pick(store)
    assert picked is not None
    assert picked.id == second_item.id  # the retried item stays held

    with pytest.raises(ConflictError):  # a retry may not switch items
        await _start(store, delivery, second_item.id, now=retry_at)
    second = await _start(store, delivery, first_item.id, now=retry_at)
    assert second.attempt_no == 2
    async with store() as uow:
        attempts = await uow.daily_deliveries.attempts_of(delivery.id)
    assert attempts[0].retry_after_seconds == 60
    assert attempts[1].finished_at is None


async def test_a_dead_attempt_is_taken_over_with_the_same_item(store: UnitOfWorkFactory) -> None:
    """Telegram took the message, the process died before recording it.

    Once the lease runs out the delivery is due again and the *same* item is sent
    once more — a possible duplicate, accepted so that the day is not lost. When
    the dead attempt reports back after all, its result is kept on the attempt
    and not applied.
    """
    await _subscriber(store)
    item, other_item = await _library(store, 2)
    delivery = await _open(store)
    dead = await _start(store, delivery, item.id)

    after_lease = NOW + LEASE + timedelta(seconds=1)
    async with store() as uow:
        due = await uow.daily_deliveries.lock_due(now=after_lease, limit=10)
    assert [entry.id for entry in due] == [delivery.id]

    with pytest.raises(ConflictError):
        await _start(store, delivery, other_item.id, now=after_lease)
    successor = await _start(store, delivery, item.id, now=after_lease)

    async with store() as uow:
        late = await uow.daily_deliveries.finish_sent(
            dead.lease_token,
            at=after_lease + timedelta(seconds=1),
            telegram_message_id=111,
        )
        sent = await uow.daily_deliveries.finish_sent(
            successor.lease_token,
            at=after_lease + timedelta(seconds=2),
            telegram_message_id=222,
        )
        attempts = await uow.daily_deliveries.attempts_of(delivery.id)

    assert late is None
    assert sent is not None
    assert sent.telegram_message_id == 222
    assert sent.attempts == 2
    first, second = attempts
    assert first.outcome is DailyAttemptOutcome.LEASE_LOST
    assert first.telegram_message_id == 111  # the duplicate is visible
    assert first.error is not None
    assert first.error.startswith("sent:")
    assert second.outcome is DailyAttemptOutcome.SENT


async def test_a_dead_attempt_nobody_hears_from_again_is_crashed(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    delivery = await _open(store)
    await _start(store, delivery, item.id)
    await _start(store, delivery, item.id, now=NOW + LEASE)

    async with store() as uow:
        first, second = await uow.daily_deliveries.attempts_of(delivery.id)
    assert first.outcome is DailyAttemptOutcome.CRASHED
    assert first.finished_at == NOW + LEASE
    assert second.outcome is None
    current = await _get(store, delivery.id)
    assert current.last_outcome is DailyAttemptOutcome.CRASHED


async def test_a_failed_delivery_gives_its_item_back(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    delivery = await _open(store)
    attempt = await _start(store, delivery, item.id)
    async with store() as uow:
        failed = await uow.daily_deliveries.finish_failed(
            attempt.lease_token,
            at=NOW,
            outcome=DailyAttemptOutcome.BLOCKED,
            error="Forbidden: bot was blocked by the user",
        )
    assert failed is not None
    assert failed.status is DailyDeliveryStatus.FAILED
    assert failed.lease_token is None
    picked = await _pick(store)
    assert picked is not None
    assert picked.id == item.id  # never delivered: offered again later


async def test_an_item_once_sent_is_never_held_again(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    today = await _open(store)
    attempt = await _start(store, today, item.id)
    async with store() as uow:
        await uow.daily_deliveries.finish_sent(attempt.lease_token, at=NOW, telegram_message_id=1)

    tomorrow = await _open(store, DAY_TWO)
    with pytest.raises(ConflictError):
        await _start(store, tomorrow, item.id, now=NOW + timedelta(days=1))
    assert await _pick(store) is None


async def test_no_content_waits_and_is_looked_at_again(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    delivery = await _open(store)
    recheck = NOW + timedelta(minutes=10)
    async with store() as uow:
        waiting = await uow.daily_deliveries.mark_no_content(
            delivery.id,
            at=NOW,
            recheck_at=recheck,
        )
        not_yet = await uow.daily_deliveries.lock_due(now=NOW + timedelta(minutes=5), limit=10)
        due = await uow.daily_deliveries.lock_due(now=recheck, limit=10)
    assert waiting.status is DailyDeliveryStatus.NO_CONTENT
    assert waiting.content_id is None
    assert not_yet == ()
    assert [item.id for item in due] == [delivery.id]

    (item,) = await _library(store, 1)
    attempt = await _start(store, delivery, item.id, now=recheck)
    assert attempt.attempt_no == 1

    with pytest.raises(ConflictError):  # it holds an item now
        async with store() as uow:
            await uow.daily_deliveries.mark_no_content(delivery.id, at=NOW, recheck_at=recheck)


async def test_cancel_closes_only_a_waiting_delivery(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    waiting = await _open(store)
    sending = await _open(store, DAY_TWO)
    await _start(store, sending, item.id, now=NOW + timedelta(days=1))
    async with store() as uow:
        cancelled = await uow.daily_deliveries.cancel(waiting.id, at=NOW)
        untouched = await uow.daily_deliveries.cancel(sending.id, at=NOW)
        again = await uow.daily_deliveries.cancel(waiting.id, at=NOW)
    assert cancelled is not None
    assert cancelled.status is DailyDeliveryStatus.CANCELLED
    assert untouched is None
    assert again is None


async def test_overdue_deliveries_expire_and_give_their_item_back(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    await _subscriber(store, OTHER)
    (item,) = await _library(store, 1)
    waiting = await _open(store)
    sending = await _open(store, user_id=OTHER)
    await _start(store, sending, item.id)

    async with store() as uow:
        expired = await uow.daily_deliveries.expire_overdue(now=waiting.deadline_at)
        again = await uow.daily_deliveries.expire_overdue(now=waiting.deadline_at)
        attempts = await uow.daily_deliveries.attempts_of(sending.id)

    assert expired == 2
    assert again == 0
    assert (await _get(store, waiting.id)).status is DailyDeliveryStatus.EXPIRED
    closed = await _get(store, sending.id)
    assert closed.status is DailyDeliveryStatus.EXPIRED
    assert closed.lease_token is None
    assert attempts[0].outcome is DailyAttemptOutcome.CRASHED
    picked = await _pick(store, OTHER)
    assert picked is not None  # the item it held is offered again


async def test_a_definite_refusal_gives_the_item_back_and_keeps_the_day(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    first_item, second_item = await _library(store, 2)
    delivery = await _open(store)
    attempt = await _start(store, delivery, first_item.id)
    async with store() as uow:
        reopened = await uow.daily_deliveries.finish_rejected(
            attempt.lease_token,
            at=NOW,
            outcome=DailyAttemptOutcome.PERMANENT,
            error="Bad Request: something new",
            next_attempt_at=NOW,
        )
        replay = await uow.daily_deliveries.finish_rejected(
            attempt.lease_token,
            at=NOW,
            outcome=DailyAttemptOutcome.PERMANENT,
            error="again",
            next_attempt_at=NOW,
        )
    assert reopened is not None
    assert reopened.status is DailyDeliveryStatus.PENDING
    assert reopened.content_id is None
    assert reopened.lease_token is None
    assert reopened.last_outcome is DailyAttemptOutcome.PERMANENT
    assert replay is None

    # Another item may go out now, and the refused one is offered again later.
    second = await _start(store, delivery, second_item.id)
    assert second.attempt_no == 2
    picked = await _pick(store)
    assert picked is not None
    assert picked.id == first_item.id
    async with store() as uow:
        first, _ = await uow.daily_deliveries.attempts_of(delivery.id)
    assert first.outcome is DailyAttemptOutcome.PERMANENT


async def test_cancel_closes_a_dead_attempt_too(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    (item,) = await _library(store, 1)
    delivery = await _open(store)
    await _start(store, delivery, item.id)
    async with store() as uow:
        live = await uow.daily_deliveries.cancel(delivery.id, at=NOW + LEASE / 2)
        dead = await uow.daily_deliveries.cancel(delivery.id, at=NOW + LEASE)
        (attempt,) = await uow.daily_deliveries.attempts_of(delivery.id)
    assert live is None  # still in flight
    assert dead is not None
    assert dead.status is DailyDeliveryStatus.CANCELLED
    assert dead.lease_token is None
    assert attempt.outcome is DailyAttemptOutcome.CRASHED
    picked = await _pick(store)
    assert picked is not None  # cancelled: the item is offered again


async def _counts(store: UnitOfWorkFactory, user_id: int = BUYER) -> tuple[int, int]:
    """(received, still available) — what the buyer's status shows."""
    async with store() as uow:
        return (
            await uow.daily_deliveries.count_sent(user_id),
            await uow.daily_content.count_available_for(user_id),
        )


async def test_the_status_counts_follow_what_was_sent_and_what_is_held(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    await _subscriber(store, OTHER)
    items = await _library(store, 4)
    async with store() as uow:
        await uow.daily_content.set_status(items[3].id, DailyContentStatus.DISABLED, at=NOW)
    assert await _counts(store) == (0, 3)

    today = await _open(store)
    attempt = await _start(store, today, items[0].id)
    assert await _counts(store) == (0, 2)  # held while it is being sent

    async with store() as uow:
        await uow.daily_deliveries.finish_sent(attempt.lease_token, at=NOW, telegram_message_id=1)
    assert await _counts(store) == (1, 2)

    later = NOW + timedelta(days=1)
    tomorrow = await _open(store, DAY_TWO)
    refused = await _start(store, tomorrow, items[1].id, now=later)
    async with store() as uow:
        await uow.daily_deliveries.finish_failed(
            refused.lease_token,
            at=later,
            outcome=DailyAttemptOutcome.BLOCKED,
            error="Forbidden: bot was blocked by the user",
        )
    assert await _counts(store) == (1, 2)  # a failed send gives the item back
    assert await _counts(store, OTHER) == (0, 3)  # counts are per buyer


# --------------------------------------------------------------------------- #
# PostgreSQL only: concurrency and the schema's own checks                    #
# --------------------------------------------------------------------------- #


async def test_the_same_text_from_two_messages_at_once_is_added_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Both would read "no such text yet" if nothing queued them; the lock does."""
    gate = Gate(2)
    racing = gated(live_uow_factory, gate, repository="daily_content", method="add")

    async def add(number: int) -> Any:
        async with racing() as uow:
            return await uow.daily_content.add(_text(number))

    results = await all_at_once(add(1), add(2))

    added = [result for result in results if isinstance(result, tuple)]
    refused = [result for result in results if isinstance(result, DailyContentDuplicateError)]
    assert len(added) == 1
    assert len(refused) == 1
    assert refused[0].existing_id == added[0][0].id
    async with live_uow_factory() as uow:
        assert (await uow.daily_content.stats()).active == 1


async def test_the_same_message_twice_at_once_is_one_item(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    racing = gated(live_uow_factory, Gate(2), repository="daily_content", method="add")

    async def add() -> Any:
        async with racing() as uow:
            return await uow.daily_content.add(_draft(1))

    first, second = await all_at_once(add(), add())

    assert {first[1], second[1]} == {True, False}
    assert first[0].id == second[0].id


async def test_the_same_file_from_two_messages_at_once_is_added_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    racing = gated(live_uow_factory, Gate(2), repository="daily_content", method="add")

    async def add(number: int) -> Any:
        async with racing() as uow:
            return await uow.daily_content.add(replace(_draft(number), file_unique_id="same"))

    results = await all_at_once(add(1), add(2))

    assert sum(1 for result in results if isinstance(result, tuple)) == 1
    assert sum(1 for result in results if isinstance(result, DailyContentDuplicateError)) == 1


async def test_concurrent_planners_open_one_delivery(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    window = window_for(NOON, DAY_ONE, recovery=RECOVERY)

    async def plan() -> DailyDelivery | None:
        async with live_uow_factory() as uow:
            return await uow.daily_deliveries.open(
                DailyDeliveryDraft(
                    user_id=BUYER,
                    trigger=DailyDeliveryTrigger.SCHEDULE,
                    window=window,
                    first_attempt_at=window.center,
                )
            )

    opened = await asyncio.gather(*(plan() for _ in range(8)))
    assert sum(1 for delivery in opened if delivery is not None) == 1


async def test_concurrent_workers_never_take_the_same_delivery(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Two transactions locking due deliveries at once get disjoint sets."""
    for user_id in range(201, 207):
        await _subscriber(live_uow_factory, user_id)
        await _open(live_uow_factory, user_id=user_id)

    first_has_locked = asyncio.Event()
    release_first = asyncio.Event()
    taken: dict[str, list[int]] = {}

    async def first_worker() -> None:
        async with live_uow_factory() as uow:
            due = await uow.daily_deliveries.lock_due(now=NOW, limit=3)
            taken["first"] = [item.id for item in due]
            first_has_locked.set()
            await release_first.wait()

    async def second_worker() -> None:
        await first_has_locked.wait()
        async with live_uow_factory() as uow:
            due = await uow.daily_deliveries.lock_due(now=NOW, limit=10)
            taken["second"] = [item.id for item in due]
        release_first.set()

    await asyncio.gather(first_worker(), second_worker())

    assert len(taken["first"]) == 3
    assert len(taken["second"]) == 3
    assert not set(taken["first"]) & set(taken["second"])


async def test_two_takeovers_of_one_dead_attempt_cannot_both_win(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The row lock serialises them; the loser finds a live lease and is refused."""
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    delivery = await _open(live_uow_factory)
    await _start(live_uow_factory, delivery, item.id)
    after_lease = NOW + LEASE + timedelta(seconds=1)

    async def take_over() -> str:
        try:
            await _start(live_uow_factory, delivery, item.id, now=after_lease)
        except ConflictError:
            return "refused"
        return "won"

    results = await asyncio.gather(*(take_over() for _ in range(5)))
    assert sorted(results) == ["refused"] * 4 + ["won"]


async def test_the_database_refuses_shapes_the_code_never_writes(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    (item,) = await _library(live_uow_factory, 1)
    delivery = await _open(live_uow_factory)

    statements = {
        "sent without a message id": (
            "UPDATE daily_deliveries SET status = 'sent', content_id = :item, sent_at = now() "
            "WHERE id = :delivery"
        ),
        "a lease outside sending": (
            "UPDATE daily_deliveries SET lease_token = gen_random_uuid(), "
            "lease_until = now() WHERE id = :delivery"
        ),
        "an anchor outside the day": (
            "UPDATE daily_subscriptions SET anchor_minute = 1440 WHERE user_id = :user"
        ),
        "an active subscription without an anchor": (
            "UPDATE daily_subscriptions SET anchor_minute = NULL WHERE user_id = :user"
        ),
        "a media item without a file": ("UPDATE daily_content SET file_id = NULL WHERE id = :item"),
    }
    for reason, statement in statements.items():
        with pytest.raises(IntegrityError):
            async with live_uow_factory() as uow:
                await uow.session.execute(
                    text(statement),
                    {"item": item.id, "delivery": delivery.id, "user": BUYER},
                )
        assert reason  # each statement is refused on its own


async def test_deliverable_and_count_completed_agree(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """One predicate: the batch query and the per-buyer count never disagree."""
    for user_id, completed in ((301, 14), (302, 15), (303, 16)):
        await _subscriber(live_uow_factory, user_id, completed=completed)
    # A refund after delivery keeps counting.
    async with live_uow_factory() as uow:
        result = await uow.session.execute(
            text(
                "UPDATE purchases SET status = 'refunded' WHERE id = "
                "(SELECT id FROM purchases WHERE user_id = 302 LIMIT 1)"
            )
        )
        assert result.rowcount == 1  # type: ignore[attr-defined]

    async with live_uow_factory() as uow:
        counts = {
            user_id: await uow.loyalty.count_completed(user_id) for user_id in (301, 302, 303)
        }
        deliverable = await uow.daily_subscriptions.deliverable(
            min_completed=DAILY_ACCESS_MILESTONE,
        )
    assert counts == {301: 14, 302: 15, 303: 16}
    assert [item.user_id for item in deliverable] == [
        user_id for user_id, count in counts.items() if count >= DAILY_ACCESS_MILESTONE
    ]
