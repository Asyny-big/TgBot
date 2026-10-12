"""The admin panel's Daily Content service over PostgreSQL.

Every change the panel makes goes through the rule it belongs to; these tests
check the rules hold — attributed, idempotent, optimistic — and that they hold
when two parties act at once: two tabs, two clicks, the owner in Telegram and
an admin in the panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from app.core.config import DailySettings
from app.core.exceptions import (
    DailyContentDuplicateError,
    DailyContentNotFoundError,
    DailyIdempotencyKeyReusedError,
    DailyLibraryActionError,
    DailyNoOwnerError,
    DailyReplyTooLongError,
    DailyRequestNotFoundError,
    DailyRequestVersionConflictError,
    DailySubscriberNotFoundError,
    ValidationError,
)
from app.domain.commands import DailyContentDraft, DailyRequestDraft
from app.domain.daily import LibraryAction
from app.domain.daily_requests import REQUEST_NOTE_LIMIT, REQUEST_REPLY_TTL
from app.domain.enums import (
    DailyContentKind,
    DailyContentStatus,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
)
from app.services.daily_admin import DailyAdminService
from app.services.daily_library import DailyLibraryService, StatusOutcome
from app.services.daily_outbox import DailyOutboxService
from tests.concurrency import Gate, all_at_once, gated, warm_pool
from tests.daily_bot_harness import seed_buyer

if TYPE_CHECKING:
    from app.domain.entities import DailyContent, DailyContentRequest
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9901
OWNER = 9900
SECOND_OWNER = 9899
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def _settings(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {"enabled": True, "admin_telegram_ids": (OWNER,)}
    values.update(overrides)
    return DailySettings(**values)


def _admin(
    factory: Any,
    clock: Clock | None = None,
    *,
    library_factory: Any = None,
    **settings: Any,
) -> DailyAdminService:
    daily = _settings(**settings)
    clock = clock or Clock()
    return DailyAdminService(
        uow_factory=factory,
        settings=daily,
        library=DailyLibraryService(
            uow_factory=library_factory or factory,
            settings=daily,
            clock=clock,
        ),
        clock=clock,
    )


async def _request(factory: SqlAlchemyUnitOfWorkFactory, messages: int = 2) -> DailyContentRequest:
    await seed_buyer(factory, BUYER, completed=20)
    async with factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
        await uow.daily_subscriptions.activate(BUYER, at=NOW, anchor_minute=720)
        request, _ = await uow.daily_requests.create(
            DailyRequestDraft(
                user_id=BUYER,
                client_draft_id=uuid4(),
                purchases_at_submit=20,
                items=tuple(
                    DailyContentDraft(
                        kind=DailyContentKind.PHOTO,
                        source_chat_id=BUYER,
                        source_message_id=number,
                        added_by=BUYER,
                        file_id=f"photo-{number}",
                        file_unique_id=f"photo-unique-{number}",
                    )
                    for number in range(1, messages + 1)
                ),
            ),
            at=NOW,
        )
    return request


async def _item(
    factory: SqlAlchemyUnitOfWorkFactory,
    number: int = 1,
    *,
    status: DailyContentStatus = DailyContentStatus.ACTIVE,
) -> DailyContent:
    async with factory() as uow:
        item, _ = await uow.daily_content.add(
            DailyContentDraft(
                kind=DailyContentKind.VIDEO,
                source_chat_id=OWNER,
                source_message_id=number,
                added_by=OWNER,
                file_id=f"video-{number}",
                file_unique_id=f"video-unique-{number}",
            )
        )
        if status is not DailyContentStatus.ACTIVE:
            item = await uow.daily_content.set_status(item.id, status, at=NOW, reason="seed")
    return item


async def _content(factory: SqlAlchemyUnitOfWorkFactory, content_id: int) -> DailyContent:
    async with factory() as uow:
        item = await uow.daily_content.get(content_id)
    assert item is not None
    return item


# --------------------------------------------------------------------------- #
# Requests — status and note                                                 #
# --------------------------------------------------------------------------- #


async def test_a_change_names_its_version_and_is_written_into_the_history(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    clock = Clock()
    admin = _admin(live_uow_factory, clock)
    clock.now = NOW + timedelta(hours=1)

    detail = await admin.update_request(
        "alice",
        request.id,
        version=1,
        status=DailyRequestStatus.IN_REVIEW,
        admin_note="  ask for the source  ",
    )

    changed = detail.entry.request
    assert (changed.version, changed.status, changed.admin_note) == (
        2,
        DailyRequestStatus.IN_REVIEW,
        "ask for the source",
    )
    assert [(event.kind, event.actor) for event in detail.events] == [
        (DailyRequestEventKind.SUBMITTED, f"user:{BUYER}"),
        (DailyRequestEventKind.STATUS_CHANGED, "admin:alice"),
        (DailyRequestEventKind.NOTE_CHANGED, "admin:alice"),
    ]
    status_line = detail.events[1]
    assert (status_line.from_status, status_line.to_status) == (
        DailyRequestStatus.NEW,
        DailyRequestStatus.IN_REVIEW,
    )
    assert detail.events[2].body == "ask for the source"

    closed = await admin.update_request(
        "bob", request.id, version=2, status=DailyRequestStatus.COMPLETED, admin_note="  "
    )
    assert closed.entry.request.closed_at == clock.now
    assert closed.entry.request.admin_note is None  # an empty note clears it
    assert closed.events[-1].body is None


async def test_a_change_from_a_stale_version_is_refused_with_the_current_one(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    admin = _admin(live_uow_factory)
    await admin.update_request("alice", request.id, version=1, status=DailyRequestStatus.IN_REVIEW)

    with pytest.raises(DailyRequestVersionConflictError) as refused:
        await admin.update_request("bob", request.id, version=1, status=DailyRequestStatus.REJECTED)

    assert refused.value.current_version == 2
    assert refused.value.details == {"request_id": request.id, "current_version": 2}
    detail = await admin.request(request.id)
    assert detail.entry.request.status is DailyRequestStatus.IN_REVIEW


async def test_a_change_that_changes_nothing_keeps_the_version(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    admin = _admin(live_uow_factory)

    same = await admin.update_request(
        "alice", request.id, version=1, status=DailyRequestStatus.NEW, admin_note=None
    )

    assert same.entry.request.version == 1
    assert [event.kind for event in same.events] == [DailyRequestEventKind.SUBMITTED]


async def test_a_note_longer_than_the_limit_is_refused(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)

    with pytest.raises(ValidationError):
        await _admin(live_uow_factory).update_request(
            "alice", request.id, version=1, admin_note="x" * (REQUEST_NOTE_LIMIT + 1)
        )


async def test_a_missing_request_is_not_found(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    admin = _admin(live_uow_factory)

    with pytest.raises(DailyRequestNotFoundError):
        await admin.request(404)
    with pytest.raises(DailyRequestNotFoundError):
        await admin.update_request("alice", 404, version=1, status=DailyRequestStatus.NEW)
    with pytest.raises(DailyRequestNotFoundError):
        await admin.reply("alice", 404, text="hi", client_key=uuid4())


async def test_two_tabs_changing_one_version_cannot_both_win(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Both tabs read version 1; the second to arrive must be told it is stale."""
    request = await _request(live_uow_factory)
    await warm_pool(live_uow_factory, 2)
    gate = Gate(2)
    held = gated(live_uow_factory, gate, repository="daily_requests", method="lock")

    results = await all_at_once(
        _admin(held).update_request(
            "alice", request.id, version=1, status=DailyRequestStatus.COMPLETED
        ),
        _admin(held).update_request(
            "bob", request.id, version=1, status=DailyRequestStatus.REJECTED
        ),
    )

    conflicts = [result for result in results if isinstance(result, BaseException)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], DailyRequestVersionConflictError)
    assert conflicts[0].current_version == 2
    detail = await _admin(live_uow_factory).request(request.id)
    assert detail.entry.request.version == 2
    changes = [
        event for event in detail.events if event.kind is DailyRequestEventKind.STATUS_CHANGED
    ]
    assert len(changes) == 1
    assert changes[0].to_status is detail.entry.request.status


# --------------------------------------------------------------------------- #
# Requests — answers                                                         #
# --------------------------------------------------------------------------- #


async def test_an_answer_is_queued_once_per_key_through_the_outbox(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    admin = _admin(live_uow_factory)
    key = uuid4()

    first = await admin.reply("alice", request.id, text="  We added it <3  ", client_key=key)
    again = await admin.reply("alice", request.id, text="We added it <3", client_key=key)

    assert (first.created, again.created) == (True, False)
    assert again.reply.event == first.reply.event
    event, message = first.reply.event, first.reply.message
    assert (event.kind, event.actor, event.body) == (
        DailyRequestEventKind.REPLY_QUEUED,
        "admin:alice",
        "We added it <3",
    )
    assert message is not None
    assert event.outbox_message_id == message.id
    assert (message.kind, message.channel, message.chat_id, message.status) == (
        DailyOutboxKind.REQUEST_REPLY,
        DailyOutboxChannel.DAILY,
        BUYER,
        DailyOutboxStatus.PENDING,
    )
    assert message.body == f"💬 Ответ на ваше пожелание #{request.id}:\n\nWe added it &lt;3"
    assert message.deadline_at == NOW + REQUEST_REPLY_TTL
    detail = await admin.request(request.id)
    assert detail.entry.reply_count == 1
    assert len(detail.replies) == 1


async def test_a_key_that_comes_back_with_another_text_is_refused(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    admin = _admin(live_uow_factory)
    key = uuid4()
    await admin.reply("alice", request.id, text="first", client_key=key)

    with pytest.raises(DailyIdempotencyKeyReusedError):
        await admin.reply("alice", request.id, text="second", client_key=key)


@pytest.mark.parametrize("text", ["", "   ", "x" * 3501], ids=["empty", "blank", "too-long"])
async def test_an_answer_must_hold_1_to_3500_characters(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    text: str,
) -> None:
    request = await _request(live_uow_factory)

    with pytest.raises(ValidationError):
        await _admin(live_uow_factory).reply("alice", request.id, text=text, client_key=uuid4())


async def test_an_answer_too_long_once_formatted_is_refused(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)

    with pytest.raises(DailyReplyTooLongError):
        await _admin(live_uow_factory).reply(
            "alice", request.id, text="<" * 3500, client_key=uuid4()
        )


async def test_the_same_answer_sent_twice_at_once_is_queued_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    await warm_pool(live_uow_factory, 2)
    gate = Gate(2)
    held = gated(live_uow_factory, gate, repository="daily_requests", method="lock")
    key = uuid4()

    results = await all_at_once(
        _admin(held).reply("alice", request.id, text="Thanks!", client_key=key),
        _admin(held).reply("alice", request.id, text="Thanks!", client_key=key),
    )

    assert not [result for result in results if isinstance(result, BaseException)]
    assert sorted(result.created for result in results) == [False, True]
    async with live_uow_factory() as uow:
        events = await uow.daily_requests.events_of(request.id)
    replies = [event for event in events if event.kind is DailyRequestEventKind.REPLY_QUEUED]
    assert len(replies) == 1


async def test_the_answers_fate_is_shown_with_the_request(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    request = await _request(live_uow_factory)
    clock = Clock()
    admin = _admin(live_uow_factory, clock)
    await admin.reply("alice", request.id, text="Thanks!", client_key=uuid4())

    class Sender:
        async def send_text(self, chat_id: int, text: str) -> int:
            del chat_id, text
            return 1

    await DailyOutboxService(
        uow_factory=live_uow_factory,
        settings=_settings(),
        senders={DailyOutboxChannel.DAILY: Sender()},
        clock=clock,
    ).process_due()
    detail = await admin.request(request.id)

    (reply,) = detail.replies
    assert reply.message is not None
    assert reply.message.status is DailyOutboxStatus.SENT
    assert reply.outcome is not None
    assert reply.outcome.kind is DailyRequestEventKind.REPLY_SENT


# --------------------------------------------------------------------------- #
# "Send it to me"                                                             #
# --------------------------------------------------------------------------- #


async def test_a_library_item_is_queued_for_every_owner_account_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    item = await _item(live_uow_factory)
    admin = _admin(live_uow_factory, admin_telegram_ids=(OWNER, SECOND_OWNER))
    key = uuid4()

    first = await admin.send_library_item("alice", item.id, key)
    again = await admin.send_library_item("alice", item.id, key)
    other_click = await admin.send_library_item("alice", item.id, uuid4())

    assert (first.created, again.created, other_click.created) == (True, False, True)
    assert [message.id for message in again.messages] == [message.id for message in first.messages]
    assert [(message.chat_id, message.content_id) for message in first.messages] == [
        (OWNER, item.id),
        (SECOND_OWNER, item.id),
    ]
    assert {message.kind for message in first.messages} == {DailyOutboxKind.LIBRARY_PREVIEW}


async def test_sending_to_the_owner_needs_an_owner_and_an_item(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    item = await _item(live_uow_factory)
    request = await _request(live_uow_factory)

    with pytest.raises(DailyNoOwnerError):
        await _admin(live_uow_factory, admin_telegram_ids=()).send_library_item(
            "alice", item.id, uuid4()
        )
    with pytest.raises(DailyContentNotFoundError):
        await _admin(live_uow_factory).send_library_item("alice", item.id + 1000, uuid4())
    async with live_uow_factory() as uow:
        (attachment, _) = await uow.daily_requests.items_of(request.id)
    with pytest.raises(DailyRequestNotFoundError):  # an item of another request
        await _admin(live_uow_factory).send_request_item(
            "alice", request.id + 1000, attachment.id, uuid4()
        )
    sent = await _admin(live_uow_factory).send_request_item(
        "alice", request.id, attachment.id, uuid4()
    )
    assert [(message.kind, message.request_item_id) for message in sent.messages] == [
        (DailyOutboxKind.REQUEST_ATTACHMENT, attachment.id),
    ]


# --------------------------------------------------------------------------- #
# The library: the owner and an admin at once                                 #
# --------------------------------------------------------------------------- #


async def test_an_admins_change_to_the_library_is_attributed(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    item = await _item(live_uow_factory)
    admin = _admin(live_uow_factory)

    disabled = await admin.change_library_status("alice", item.id, LibraryAction.DISABLE)
    again = await admin.change_library_status("bob", item.id, LibraryAction.DISABLE)

    assert disabled.item.status is DailyContentStatus.DISABLED
    assert disabled.item.status_reason == "admin:alice:disable"
    assert again.item.status_reason == "admin:alice:disable"  # unchanged, not re-attributed
    with pytest.raises(DailyLibraryActionError):
        await admin.change_library_status("alice", item.id, LibraryAction.RESTORE)
    with pytest.raises(DailyContentNotFoundError):
        await admin.change_library_status("alice", item.id + 1000, LibraryAction.DISABLE)


async def test_restoring_a_duplicate_is_refused_with_the_live_twin(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    archived = await _item(live_uow_factory, 1, status=DailyContentStatus.ARCHIVED)
    async with live_uow_factory() as uow:  # the same file came back as a new item
        twin, _ = await uow.daily_content.add(
            DailyContentDraft(
                kind=DailyContentKind.VIDEO,
                source_chat_id=OWNER,
                source_message_id=2,
                added_by=OWNER,
                file_id="video-1",
                file_unique_id="video-unique-1",
            )
        )

    with pytest.raises(DailyContentDuplicateError) as refused:
        await _admin(live_uow_factory).change_library_status(
            "alice", archived.id, LibraryAction.RESTORE
        )

    assert refused.value.existing_id == twin.id


async def test_the_owner_and_an_admin_restoring_one_item_at_once_restore_it_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The first restorer is held with its change made but not committed.

    The second must wait for it on the item's row and then find the item
    restored; deciding on an unlocked read, it would "restore" it again.
    """
    item = await _item(live_uow_factory, status=DailyContentStatus.ARCHIVED)
    await warm_pool(live_uow_factory, 2)
    gate = Gate(2)
    held = gated(live_uow_factory, gate, repository="daily_content", method="set_status")
    settings = _settings()
    owner = DailyLibraryService(uow_factory=held, settings=settings, clock=Clock())
    admin = DailyLibraryService(uow_factory=held, settings=settings, clock=Clock())

    results = await all_at_once(
        owner.change_status(OWNER, item.id, LibraryAction.RESTORE),
        admin.change_status_as_admin("alice", item.id, LibraryAction.RESTORE),
    )

    assert not [result for result in results if isinstance(result, BaseException)]
    assert sorted(result.outcome for result in results) == [
        StatusOutcome.CHANGED,
        StatusOutcome.UNCHANGED,
    ]
    restored = await _content(live_uow_factory, item.id)
    assert restored.status is DailyContentStatus.ACTIVE
    changed = next(result for result in results if result.outcome is StatusOutcome.CHANGED)
    assert restored.status_reason in {"owner:restore", "admin:alice:restore"}
    assert changed.item is not None
    assert changed.item.status_reason == restored.status_reason


async def test_an_owners_disable_and_an_admins_archive_end_as_one_order_would(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    item = await _item(live_uow_factory)
    await warm_pool(live_uow_factory, 2)
    gate = Gate(2)
    held = gated(live_uow_factory, gate, repository="daily_content", method="lock")
    settings = _settings()
    owner = DailyLibraryService(uow_factory=held, settings=settings, clock=Clock())
    admin = DailyLibraryService(uow_factory=held, settings=settings, clock=Clock())

    owner_result, admin_result = await all_at_once(
        owner.change_status(OWNER, item.id, LibraryAction.DISABLE),
        admin.change_status_as_admin("alice", item.id, LibraryAction.ARCHIVE),
    )

    final = await _content(live_uow_factory, item.id)
    # Either the owner first (disabled, then archived) or the admin first (the
    # owner's disable then finds an archived item and is refused); never a mix.
    assert final.status is DailyContentStatus.ARCHIVED
    assert final.status_reason == "admin:alice:archive"
    assert admin_result.outcome is StatusOutcome.CHANGED
    assert owner_result.outcome in {StatusOutcome.CHANGED, StatusOutcome.NOT_ALLOWED}


# --------------------------------------------------------------------------- #
# Subscribers, deliveries, overview                                           #
# --------------------------------------------------------------------------- #


async def test_a_ban_is_attributed_idempotent_and_can_be_lifted(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _request(live_uow_factory)
    clock = Clock()
    admin = _admin(live_uow_factory, clock)

    banned = await admin.set_requests_ban("alice", BUYER, banned=True)
    clock.now = NOW + timedelta(hours=1)
    again = await admin.set_requests_ban("bob", BUYER, banned=True)
    lifted = await admin.set_requests_ban("bob", BUYER, banned=False)

    assert banned.entry.subscription.requests_banned_by == "admin:alice"
    assert again.entry.subscription.requests_banned_by == "admin:alice"
    assert again.entry.subscription.requests_banned_at == NOW
    assert lifted.entry.subscription.requests_banned_at is None
    assert [request.user_id for request in lifted.requests] == [BUYER]
    with pytest.raises(DailySubscriberNotFoundError):
        await admin.set_requests_ban("alice", BUYER + 1000, banned=True)
    with pytest.raises(DailySubscriberNotFoundError):
        await admin.subscriber(BUYER + 1000)


async def test_the_overview_counts_the_moscow_day(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _request(live_uow_factory)
    await _item(live_uow_factory)
    clock = Clock(datetime(2026, 10, 11, 22, 30, tzinfo=UTC))  # 01:30 on the 12th in Moscow

    overview = await _admin(live_uow_factory, clock).overview()

    assert overview.enabled
    assert str(overview.today) == "2026-10-12"
    assert overview.library.active == 1
    assert overview.counts.requests[DailyRequestStatus.NEW] == 1
