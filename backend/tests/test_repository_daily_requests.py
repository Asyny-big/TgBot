"""Content requests' repositories: one contract, two implementations.

The first part runs against the fakes *and* PostgreSQL, so the fakes the
service tests use cannot drift from the database. The rest is PostgreSQL only:
the constraints, the admin panel's reads, and two transactions made to
collide on purpose.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.domain.commands import (
    DailyContentDraft,
    DailyDeliveryDraft,
    DailyOutboxDraft,
    DailyRequestDraft,
    DailyRequestEventDraft,
    UserDraft,
)
from app.domain.daily import window_for
from app.domain.daily_reports import (
    DailyDeliveryFilters,
    DailyLibraryFilters,
    DailyRequestFilters,
    DailySubscriberFilters,
)
from app.domain.enums import (
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
    DailySubscriptionStatus,
)
from app.domain.pagination import PageRequest
from app.domain.patch import UNSET
from tests.concurrency import all_at_once, warm_pool
from tests.daily_bot_harness import seed_buyer
from tests.fakes import FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from app.domain.entities import DailyContentRequest
    from app.domain.uow import UnitOfWorkFactory
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9801
OTHER = 9802
OWNER = 9800
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # 12:00 in Moscow
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
    completed: int = 20,
) -> None:
    await seed_buyer(store, user_id, completed=completed)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=720)


def _message(number: int, *, user_id: int = BUYER, photo: bool = False) -> DailyContentDraft:
    if photo:
        return DailyContentDraft(
            kind=DailyContentKind.PHOTO,
            source_chat_id=user_id,
            source_message_id=number,
            added_by=user_id,
            body="caption",
            entities=({"type": "italic", "offset": 0, "length": 7},),
            file_id=f"photo-{number}",
            file_unique_id=f"photo-unique-{number}",
            file_size=1_000,
        )
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=user_id,
        source_message_id=number,
        added_by=user_id,
        body=f"idea {number}",
    )


def _draft(
    user_id: int = BUYER,
    *,
    client: UUID | None = None,
    messages: int = 2,
) -> DailyRequestDraft:
    return DailyRequestDraft(
        user_id=user_id,
        client_draft_id=client or uuid4(),
        purchases_at_submit=20,
        items=tuple(_message(number, photo=number % 2 == 0) for number in range(1, messages + 1)),
    )


async def _create(
    store: UnitOfWorkFactory,
    draft: DailyRequestDraft | None = None,
    *,
    at: datetime = NOW,
) -> DailyContentRequest:
    async with store() as uow:
        request, created = await uow.daily_requests.create(draft or _draft(), at=at)
    assert created
    return request


def _reply_draft(request_id: int, message_id: int, key: str) -> DailyRequestEventDraft:
    return DailyRequestEventDraft(
        request_id=request_id,
        kind=DailyRequestEventKind.REPLY_QUEUED,
        actor="admin:root",
        body="thanks",
        outbox_message_id=message_id,
        idempotency_key=key,
    )


async def _queue(  # noqa: PLR0913 — one message, this many facts
    store: UnitOfWorkFactory,
    key: str,
    *,
    chat_id: int = BUYER,
    kind: DailyOutboxKind = DailyOutboxKind.REQUEST_REPLY,
    content_id: int | None = None,
    request_item_id: int | None = None,
) -> int:
    async with store() as uow:
        message = await uow.daily_outbox.enqueue(
            DailyOutboxDraft(
                kind=kind,
                channel=DailyOutboxChannel.DAILY,
                chat_id=chat_id,
                body=f"message {key}",
                dedupe_key=key,
                first_attempt_at=NOW,
                deadline_at=NOW + timedelta(days=3),
                content_id=content_id,
                request_item_id=request_item_id,
            )
        )
    assert message is not None
    return message.id


# --------------------------------------------------------------------------- #
# The contract                                                                #
# --------------------------------------------------------------------------- #


async def test_a_draft_becomes_one_request_with_its_messages_and_history(
    store: UnitOfWorkFactory,
) -> None:
    await _subscriber(store)
    draft = _draft(messages=3)

    request = await _create(store, draft)
    async with store() as uow:
        again, created = await uow.daily_requests.create(draft, at=NOW + timedelta(minutes=5))
        items = await uow.daily_requests.items_of(request.id)
        events = await uow.daily_requests.events_of(request.id)
        found = await uow.daily_requests.find_by_draft(draft.client_draft_id)

    assert not created
    assert again == request == found
    assert (request.status, request.version, request.user_id) == (
        DailyRequestStatus.NEW,
        1,
        BUYER,
    )
    assert (request.created_at, request.closed_at, request.admin_note) == (NOW, None, None)
    assert [(item.position, item.kind, item.source_message_id) for item in items] == [
        (1, DailyContentKind.TEXT, 1),
        (2, DailyContentKind.PHOTO, 2),
        (3, DailyContentKind.TEXT, 3),
    ]
    assert items[1].entities == ({"type": "italic", "offset": 0, "length": 7},)
    assert items[1].file_id == "photo-2"
    assert [(event.kind, event.actor, event.to_status) for event in events] == [
        (DailyRequestEventKind.SUBMITTED, f"user:{BUYER}", DailyRequestStatus.NEW),
    ]


async def test_a_draft_cannot_become_another_buyers_request(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    await _subscriber(store, OTHER)
    client = uuid4()
    await _create(store, _draft(client=client))

    with pytest.raises(ConflictError):
        async with store() as uow:
            await uow.daily_requests.create(_draft(OTHER, client=client), at=NOW)


async def test_open_requests_and_recent_ones_are_counted(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    await _subscriber(store, OTHER)
    first = await _create(store, at=NOW - timedelta(hours=30))
    await _create(store, at=NOW - timedelta(hours=2))
    await _create(store, _draft(OTHER))
    async with store() as uow:
        await uow.daily_requests.update(
            first.id,
            expected_version=1,
            at=NOW,
            status=DailyRequestStatus.REJECTED,
            admin_note=UNSET,
        )
        open_count = await uow.daily_requests.count_open(BUYER)
        today = await uow.daily_requests.count_since(BUYER, NOW - timedelta(hours=24))

    assert (open_count, today) == (1, 1)


async def test_a_change_applies_only_at_the_version_it_names(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    request = await _create(store)
    later = NOW + timedelta(hours=1)

    async with store() as uow:
        reviewed = await uow.daily_requests.update(
            request.id,
            expected_version=1,
            at=later,
            status=DailyRequestStatus.IN_REVIEW,
            admin_note="looks good",
        )
        stale = await uow.daily_requests.update(
            request.id,
            expected_version=1,
            at=later,
            status=DailyRequestStatus.REJECTED,
            admin_note=UNSET,
        )
        missing = await uow.daily_requests.update(
            request.id + 1000,
            expected_version=1,
            at=later,
            status=UNSET,
            admin_note=None,
        )
    assert reviewed is not None
    assert (reviewed.version, reviewed.status, reviewed.admin_note) == (
        2,
        DailyRequestStatus.IN_REVIEW,
        "looks good",
    )
    assert (reviewed.updated_at, reviewed.closed_at) == (later, None)
    assert stale is None
    assert missing is None

    async with store() as uow:
        completed = await uow.daily_requests.update(
            request.id,
            expected_version=2,
            at=later,
            status=DailyRequestStatus.COMPLETED,
            admin_note=None,
        )
    assert completed is not None
    assert (completed.version, completed.closed_at, completed.admin_note) == (3, later, None)

    async with store() as uow:
        reopened = await uow.daily_requests.update(
            request.id,
            expected_version=3,
            at=later,
            status=DailyRequestStatus.IN_REVIEW,
            admin_note=UNSET,
        )
        current = await uow.daily_requests.get(request.id)
    assert reopened is not None
    assert reopened.closed_at is None
    assert current == reopened


async def test_a_history_line_under_a_key_is_written_once(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    request = await _create(store)
    message_id = await _queue(store, "reply:1")
    other_id = await _queue(store, "reply:2")

    async with store() as uow:
        first = await uow.daily_requests.add_event(
            _reply_draft(request.id, message_id, "reply:1"),
            at=NOW,
        )
        again = await uow.daily_requests.add_event(
            _reply_draft(request.id, message_id, "reply:1"),
            at=NOW,
        )
        sent = await uow.daily_requests.add_event(
            DailyRequestEventDraft(
                request_id=request.id,
                kind=DailyRequestEventKind.REPLY_SENT,
                actor="system",
                outbox_message_id=message_id,
                idempotency_key="reply-outcome:1",
            ),
            at=NOW + timedelta(seconds=1),
        )
        keyless = [
            await uow.daily_requests.add_event(
                DailyRequestEventDraft(
                    request_id=request.id,
                    kind=DailyRequestEventKind.NOTE_CHANGED,
                    actor="admin:root",
                    body=f"note {number}",
                ),
                at=NOW + timedelta(seconds=2),
            )
            for number in range(2)
        ]
        found = await uow.daily_requests.find_event("reply:1")
        reply = await uow.daily_requests.reply_of(message_id)
        nothing = await uow.daily_requests.reply_of(other_id)
        events = await uow.daily_requests.events_of(request.id)

    assert first is not None
    assert again is None
    assert sent is not None
    assert all(event is not None for event in keyless)  # no key: nothing to repeat
    assert found == first == reply
    assert nothing is None
    assert [event.kind for event in events] == [
        DailyRequestEventKind.SUBMITTED,
        DailyRequestEventKind.REPLY_QUEUED,
        DailyRequestEventKind.REPLY_SENT,
        DailyRequestEventKind.NOTE_CHANGED,
        DailyRequestEventKind.NOTE_CHANGED,
    ]


async def test_a_ban_keeps_who_banned_first_and_can_be_lifted(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)

    async with store() as uow:
        banned = await uow.daily_subscriptions.set_requests_ban(
            BUYER, banned_by="admin:alice", at=NOW
        )
        again = await uow.daily_subscriptions.set_requests_ban(
            BUYER, banned_by="admin:bob", at=NOW + timedelta(hours=1)
        )
        nobody = await uow.daily_subscriptions.set_requests_ban(
            OTHER, banned_by="admin:alice", at=NOW
        )
    async with store() as uow:
        lifted = await uow.daily_subscriptions.set_requests_ban(BUYER, banned_by=None, at=NOW)

    assert banned is not None
    assert (banned.requests_banned_at, banned.requests_banned_by) == (NOW, "admin:alice")
    assert again is not None
    assert (again.requests_banned_at, again.requests_banned_by) == (NOW, "admin:alice")
    assert nobody is None
    assert lifted is not None
    assert (lifted.requests_banned_at, lifted.requests_banned_by) == (None, None)


async def test_an_outbox_message_may_carry_an_item(store: UnitOfWorkFactory) -> None:
    await _subscriber(store)
    request = await _create(store)
    async with store() as uow:
        item, _ = await uow.daily_content.add(_message(50, user_id=OWNER, photo=True))
        (attachment, *_) = await uow.daily_requests.items_of(request.id)

    preview_id = await _queue(
        store,
        "preview",
        chat_id=OWNER,
        kind=DailyOutboxKind.LIBRARY_PREVIEW,
        content_id=item.id,
    )
    attachment_id = await _queue(
        store,
        "attachment",
        chat_id=OWNER,
        kind=DailyOutboxKind.REQUEST_ATTACHMENT,
        request_item_id=attachment.id,
    )
    async with store() as uow:
        preview = await uow.daily_outbox.get(preview_id)
        sent_attachment = await uow.daily_outbox.get(attachment_id)
        got = await uow.daily_requests.get_item(attachment.id)

    assert preview is not None
    assert (preview.content_id, preview.request_item_id) == (item.id, None)
    assert sent_attachment is not None
    assert (sent_attachment.content_id, sent_attachment.request_item_id) == (None, attachment.id)
    assert got == attachment


async def test_expired_messages_come_back_expired(store: UnitOfWorkFactory) -> None:
    await _queue(store, "a")
    await _queue(store, "b")

    async with store() as uow:
        expired = await uow.daily_outbox.expire_overdue(now=NOW + timedelta(days=3), limit=1)
        rest = await uow.daily_outbox.expire_overdue(now=NOW + timedelta(days=3))

    assert len(expired) == 1
    assert len(rest) == 1
    assert {message.status for message in (*expired, *rest)} == {DailyOutboxStatus.EXPIRED}
    assert {message.lease_token for message in (*expired, *rest)} == {None}


# --------------------------------------------------------------------------- #
# PostgreSQL — constraints                                                     #
# --------------------------------------------------------------------------- #


async def test_an_outbox_message_carries_at_most_one_item(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    request = await _create(live_uow_factory)
    async with live_uow_factory() as uow:
        item, _ = await uow.daily_content.add(_message(50, user_id=OWNER, photo=True))
        (attachment, *_) = await uow.daily_requests.items_of(request.id)

    with pytest.raises(IntegrityError, match="ck_daily_outbox_one_attachment"):
        await _queue(
            live_uow_factory,
            "both",
            content_id=item.id,
            request_item_id=attachment.id,
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE daily_subscriptions SET requests_banned_at = now() WHERE user_id = :user_id",
        "UPDATE daily_subscriptions SET requests_banned_by = 'admin:x' WHERE user_id = :user_id",
    ],
    ids=["when-without-who", "who-without-when"],
)
async def test_a_ban_names_both_who_and_when(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    statement: str,
) -> None:
    await _subscriber(live_uow_factory)

    with pytest.raises(IntegrityError, match="ck_daily_subscriptions_requests_ban_shape"):
        async with live_uow_factory() as uow:
            await uow.session.execute(text(statement), {"user_id": BUYER})


async def test_a_closed_request_has_a_closing_time(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    request = await _create(live_uow_factory)

    with pytest.raises(IntegrityError, match="ck_daily_requests_closed_shape"):
        async with live_uow_factory() as uow:
            await uow.session.execute(
                text("UPDATE daily_requests SET status = 'completed' WHERE id = :id"),
                {"id": request.id},
            )


# --------------------------------------------------------------------------- #
# PostgreSQL — collisions                                                      #
# --------------------------------------------------------------------------- #


async def test_one_draft_sent_twice_at_once_is_one_request(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await warm_pool(live_uow_factory, 2)
    draft = _draft()

    async def create() -> tuple[DailyContentRequest, bool]:
        async with live_uow_factory() as uow:
            return await uow.daily_requests.create(draft, at=NOW)

    results = await all_at_once(create(), create())

    assert not [result for result in results if isinstance(result, BaseException)]
    assert sorted(created for _, created in results) == [False, True]
    assert len({request.id for request, _ in results}) == 1
    async with live_uow_factory() as uow:
        count = await uow.session.scalar(text("SELECT count(*) FROM daily_requests"))
        items = await uow.session.scalar(text("SELECT count(*) FROM daily_request_items"))
    assert (count, items) == (1, 2)


async def test_two_changes_from_the_same_version_one_wins(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    request = await _create(live_uow_factory)
    await warm_pool(live_uow_factory, 2)

    async def change(status: DailyRequestStatus) -> DailyContentRequest | None:
        async with live_uow_factory() as uow:
            return await uow.daily_requests.update(
                request.id,
                expected_version=1,
                at=NOW,
                status=status,
                admin_note=UNSET,
            )

    results = await all_at_once(
        change(DailyRequestStatus.COMPLETED),
        change(DailyRequestStatus.REJECTED),
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    async with live_uow_factory() as uow:
        current = await uow.daily_requests.get(request.id)
    assert current == winners[0]
    assert current is not None
    assert current.version == 2


# --------------------------------------------------------------------------- #
# PostgreSQL — the admin panel's reads                                         #
# --------------------------------------------------------------------------- #


async def _library(store: SqlAlchemyUnitOfWorkFactory) -> list[int]:
    """Three items: a text, a named document, a disabled video."""
    drafts = [
        DailyContentDraft(
            kind=DailyContentKind.TEXT,
            source_chat_id=OWNER,
            source_message_id=1,
            added_by=OWNER,
            body="Morning stretch routine",
        ),
        DailyContentDraft(
            kind=DailyContentKind.DOCUMENT,
            source_chat_id=OWNER,
            source_message_id=2,
            added_by=OWNER,
            file_id="doc",
            file_unique_id="doc-unique",
            file_name="Recipes_100%.pdf",
        ),
        DailyContentDraft(
            kind=DailyContentKind.VIDEO,
            source_chat_id=OWNER,
            source_message_id=3,
            added_by=OWNER,
            body="evening",
            file_id="video",
            file_unique_id="video-unique",
        ),
    ]
    ids = []
    async with store() as uow:
        for draft in drafts:
            item, _ = await uow.daily_content.add(draft)
            ids.append(item.id)
        await uow.daily_content.set_status(ids[2], DailyContentStatus.DISABLED, at=NOW)
    return ids


async def _sent_delivery(
    store: SqlAlchemyUnitOfWorkFactory,
    user_id: int,
    content_id: int,
    day: date = TODAY,
) -> int:
    window = window_for(720, day, recovery=timedelta(hours=12))
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
        attempt = await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=content_id,
            now=window.center,
            lease_until=window.center + timedelta(minutes=5),
        )
        await uow.daily_deliveries.finish_sent(
            attempt.lease_token,
            at=window.center,
            telegram_message_id=1,
        )
    return delivery.id


async def test_the_library_listing_filters_searches_and_counts_receipts(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    text_id, document_id, video_id = await _library(live_uow_factory)
    await _subscriber(live_uow_factory)
    await _subscriber(live_uow_factory, OTHER)
    await _sent_delivery(live_uow_factory, BUYER, text_id)
    await _sent_delivery(live_uow_factory, OTHER, text_id)
    page = PageRequest(limit=10)

    async with live_uow_factory() as uow:
        reports = uow.daily_reports
        everything = await reports.library(DailyLibraryFilters(), page)
        disabled = await reports.library(
            DailyLibraryFilters(status=DailyContentStatus.DISABLED), page
        )
        documents = await reports.library(DailyLibraryFilters(kind=DailyContentKind.DOCUMENT), page)
        by_text = await reports.library(DailyLibraryFilters(search="STRETCH"), page)
        by_name = await reports.library(DailyLibraryFilters(search="100%"), page)
        by_number = await reports.library(DailyLibraryFilters(search=f"#{video_id}"), page)
        wildcard = await reports.library(DailyLibraryFilters(search="%"), page)
        paged = await reports.library(DailyLibraryFilters(), PageRequest(limit=2, offset=2))
        entry = await reports.library_entry(text_id)
        missing = await reports.library_entry(video_id + 1000)

    assert [row.item.id for row in everything.items] == [video_id, document_id, text_id]
    assert everything.total == 3
    assert [row.item.id for row in disabled.items] == [video_id]
    assert [row.item.id for row in documents.items] == [document_id]
    assert [row.item.id for row in by_text.items] == [text_id]
    assert [row.item.id for row in by_name.items] == [document_id]
    assert [row.item.id for row in by_number.items] == [video_id]
    assert [row.item.id for row in wildcard.items] == [document_id]  # "%" is a character
    assert ([row.item.id for row in paged.items], paged.total) == ([text_id], 3)
    assert entry is not None
    assert entry.sent_count == 2
    assert missing is None


async def test_subscribers_show_the_shared_count_and_filter_by_ban_and_block(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    (text_id, *_) = await _library(live_uow_factory)
    await _subscriber(live_uow_factory, BUYER, completed=21)
    await _subscriber(live_uow_factory, OTHER, completed=15)
    async with live_uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=BUYER, username="kate", first_name="Kate"))
        await uow.daily_subscriptions.set_requests_ban(OTHER, banned_by="admin:root", at=NOW)
        await uow.daily_subscriptions.mark_blocked(OTHER, at=NOW)
        await uow.daily_subscriptions.pause(BUYER, at=NOW)
    await _sent_delivery(live_uow_factory, BUYER, text_id)
    await _create(live_uow_factory)
    page = PageRequest(limit=10)

    async with live_uow_factory() as uow:
        reports = uow.daily_reports
        everyone = await reports.subscribers(DailySubscriberFilters(), page)
        banned = await reports.subscribers(DailySubscriberFilters(banned=True), page)
        unbanned = await reports.subscribers(DailySubscriberFilters(banned=False), page)
        blocked = await reports.subscribers(DailySubscriberFilters(blocked=True), page)
        paused = await reports.subscribers(
            DailySubscriberFilters(status=DailySubscriptionStatus.PAUSED), page
        )
        by_name = await reports.subscribers(DailySubscriberFilters(search="@KAT"), page)
        by_id = await reports.subscribers(DailySubscriberFilters(search=str(OTHER)), page)
        kate = await reports.subscriber(BUYER)

    assert everyone.total == 2
    assert [row.subscription.user_id for row in banned.items] == [OTHER]
    assert [row.subscription.user_id for row in unbanned.items] == [BUYER]
    assert [row.subscription.user_id for row in blocked.items] == [OTHER]
    assert [row.subscription.user_id for row in paused.items] == [BUYER]
    assert [row.subscription.user_id for row in by_name.items] == [BUYER]
    assert [row.subscription.user_id for row in by_id.items] == [OTHER]
    assert kate is not None
    assert (kate.buyer.username, kate.buyer.first_name) == ("kate", "Kate")
    assert (kate.completed_purchases, kate.received, kate.open_requests) == (21, 1, 1)
    assert kate.last_received_at is not None


async def test_requests_are_listed_with_their_messages_and_answers_counted(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _subscriber(live_uow_factory)
    await _subscriber(live_uow_factory, OTHER)
    older = await _create(live_uow_factory, _draft(messages=3), at=NOW - timedelta(hours=1))
    newer = await _create(live_uow_factory, _draft(OTHER, messages=1))
    message_id = await _queue(live_uow_factory, "reply:a")
    async with live_uow_factory() as uow:
        await uow.daily_requests.add_event(_reply_draft(older.id, message_id, "reply:a"), at=NOW)
        await uow.daily_requests.update(
            newer.id,
            expected_version=1,
            at=NOW,
            status=DailyRequestStatus.IN_REVIEW,
            admin_note=UNSET,
        )
    page = PageRequest(limit=10)

    async with live_uow_factory() as uow:
        reports = uow.daily_reports
        everything = await reports.requests(DailyRequestFilters(), page)
        in_review = await reports.requests(
            DailyRequestFilters(status=DailyRequestStatus.IN_REVIEW), page
        )
        theirs = await reports.requests(DailyRequestFilters(user_id=BUYER), page)
        entry = await reports.request_entry(older.id)
        latest = await reports.requests_of(BUYER, limit=5)

    assert [row.request.id for row in everything.items] == [newer.id, older.id]
    assert [row.request.id for row in in_review.items] == [newer.id]
    assert [row.request.id for row in theirs.items] == [older.id]
    assert entry is not None
    assert (entry.item_count, entry.reply_count) == (3, 1)
    assert [request.id for request in latest] == [older.id]


async def test_deliveries_are_listed_by_date_status_and_buyer(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    (text_id, document_id, _) = await _library(live_uow_factory)
    await _subscriber(live_uow_factory)
    await _subscriber(live_uow_factory, OTHER)
    yesterday = await _sent_delivery(
        live_uow_factory, BUYER, text_id, day=TODAY - timedelta(days=1)
    )
    today = await _sent_delivery(live_uow_factory, BUYER, document_id)
    theirs = await _sent_delivery(live_uow_factory, OTHER, text_id)
    page = PageRequest(limit=10)

    async with live_uow_factory() as uow:
        reports = uow.daily_reports
        everything = await reports.deliveries(DailyDeliveryFilters(), page)
        of_today = await reports.deliveries(DailyDeliveryFilters(delivery_date=TODAY), page)
        of_buyer = await reports.deliveries(DailyDeliveryFilters(user_id=BUYER), page)
        failed = await reports.deliveries(
            DailyDeliveryFilters(status=DailyDeliveryStatus.FAILED), page
        )
        one = await reports.delivery(today)
        recent = await reports.recent_deliveries(BUYER, limit=1)

    assert [row.delivery.id for row in everything.items] == [theirs, today, yesterday]
    assert {row.delivery.id for row in of_today.items} == {today, theirs}
    assert [row.delivery.id for row in of_buyer.items] == [today, yesterday]
    assert failed.items == ()
    assert one is not None
    assert one.delivery.content_id == document_id
    assert [delivery.id for delivery in recent] == [today]


async def test_the_overview_counts_today_in_moscow(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    (text_id, *_) = await _library(live_uow_factory)
    await _subscriber(live_uow_factory)
    await _subscriber(live_uow_factory, OTHER)
    async with live_uow_factory() as uow:
        await uow.daily_subscriptions.pause(OTHER, at=NOW)
        await uow.daily_subscriptions.set_requests_ban(OTHER, banned_by="admin:root", at=NOW)
    await _sent_delivery(live_uow_factory, BUYER, text_id)
    await _sent_delivery(live_uow_factory, OTHER, text_id, day=TODAY - timedelta(days=1))
    await _create(live_uow_factory)
    await _queue(live_uow_factory, "open")
    failed_id = await _queue(live_uow_factory, "failed")
    async with live_uow_factory() as uow:
        await uow.session.execute(
            text("UPDATE daily_outbox SET status = 'failed' WHERE id = :id"),
            {"id": failed_id},
        )

    async with live_uow_factory() as uow:
        counts = await uow.daily_reports.overview_counts(
            today=TODAY,
            since=datetime.now(UTC) - timedelta(hours=24),
        )

    assert counts.subscriptions[DailySubscriptionStatus.ACTIVE] == 1
    assert counts.subscriptions[DailySubscriptionStatus.PAUSED] == 1
    assert (counts.blocked, counts.banned) == (0, 1)
    assert counts.deliveries_today[DailyDeliveryStatus.SENT] == 1
    assert counts.requests[DailyRequestStatus.NEW] == 1
    assert (counts.outbox_open, counts.outbox_failed_recently) == (1, 1)
