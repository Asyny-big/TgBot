"""Buyers' content requests on the in-memory fakes: the request service's
decisions, and the outbox recording what became of the owner's answers and of
the items sent to the owner from the admin panel.

The fakes keep the database's rules (see ``test_repository_daily_requests``);
the bot's flow over PostgreSQL and Redis is in ``test_daily_requests_flow``,
the admin panel's in ``test_service_daily_admin`` and ``test_api_daily``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from app.core.config import DailySettings
from app.core.exceptions import (
    DailySendBlockedError,
    DailySendError,
    DailySendTransientError,
)
from app.domain.commands import DailyContentDraft, DailyOutboxDraft
from app.domain.daily_requests import (
    REQUEST_MAX_ITEMS,
    REQUEST_REPLY_TTL,
    REQUEST_TEXT_LIMIT,
    RequestAccess,
)
from app.domain.enums import (
    DailyContentKind,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
)
from app.domain.patch import UNSET
from app.services.daily_admin import DailyAdminService
from app.services.daily_library import DailyLibraryService
from app.services.daily_outbox import DailyOutboxService, OutboxStep
from app.services.daily_requests import DailyRequestService, SubmitOutcome, submit_lock_key
from tests.concurrency import Gate, all_at_once, gated, warm_pool
from tests.daily_bot_harness import seed_buyer
from tests.fakes import FakeLockManager, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.domain.entities import DailyContent, DailyContentRequest
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9701
OTHER = 9702
OWNER = 9700
SECOND_OWNER = 9699
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def _settings(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "admin_telegram_ids": (OWNER,),
        "max_attempts": 3,
        "retry_initial_seconds": 30,
        "retry_max_seconds": 900,
    }
    values.update(overrides)
    return DailySettings(**values)


def _requests(
    store: FakeUnitOfWorkFactory,
    clock: Clock,
    locks: FakeLockManager | None = None,
    **settings: Any,
) -> DailyRequestService:
    return DailyRequestService(
        uow_factory=store,
        settings=_settings(**settings),
        locks=locks or FakeLockManager(),
        clock=clock,
    )


async def _subscriber(
    store: FakeUnitOfWorkFactory,
    user_id: int = BUYER,
    *,
    completed: int = 20,
    activate: bool = True,
) -> None:
    await seed_buyer(store, user_id, completed=completed)
    async with store() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        if activate:
            await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=720)


def _text(number: int, body: str | None = None, *, user_id: int = BUYER) -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=user_id,
        source_message_id=number,
        added_by=user_id,
        body=body if body is not None else f"idea {number}",
    )


def _photo(number: int, *, user_id: int = BUYER) -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.PHOTO,
        source_chat_id=user_id,
        source_message_id=number,
        added_by=user_id,
        body="look at this",
        file_id=f"photo-{number}",
        file_unique_id=f"photo-unique-{number}",
        file_size=98_000,
    )


async def _submit(
    service: DailyRequestService,
    *items: DailyContentDraft,
    user_id: int = BUYER,
    draft: UUID | None = None,
) -> Any:
    return await service.submit(
        user_id,
        client_draft_id=draft or uuid4(),
        items=items or (_text(1),),
    )


# --------------------------------------------------------------------------- #
# Who may suggest                                                             #
# --------------------------------------------------------------------------- #


async def test_a_buyer_with_twenty_completed_purchases_may_suggest() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)

    access = await _requests(store, clock).access(BUYER)

    assert access.access is RequestAccess.ALLOWED
    assert access.completed_purchases == 20
    assert access.can_suggest


async def test_nineteen_purchases_are_not_enough_and_the_count_is_told() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store, completed=19)

    access = await _requests(store, clock).access(BUYER)

    assert access.access is RequestAccess.NOT_ELIGIBLE
    assert access.completed_purchases == 19
    assert not access.can_suggest


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("disabled", RequestAccess.DISABLED),
        ("outside_beta", RequestAccess.DISABLED),
        ("owner", RequestAccess.OWNER),
        ("never_wrote", RequestAccess.NOT_SUBSCRIBED),
        ("never_activated", RequestAccess.NOT_SUBSCRIBED),
        ("banned", RequestAccess.BANNED),
    ],
)
async def test_access_is_refused_for_each_reason(case: str, expected: RequestAccess) -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    settings: dict[str, Any] = {}
    user_id = BUYER
    match case:
        case "disabled":
            await _subscriber(store)
            settings["enabled"] = False
        case "outside_beta":
            await _subscriber(store)
            settings["beta_user_ids"] = (OTHER,)
        case "owner":
            user_id = OWNER
            await seed_buyer(store, OWNER, completed=25)
        case "never_wrote":
            await seed_buyer(store, BUYER, completed=25)
        case "never_activated":
            await _subscriber(store, activate=False)
        case "banned":
            await _subscriber(store)
            async with store() as uow:
                await uow.daily_subscriptions.set_requests_ban(
                    BUYER, banned_by="admin:root", at=NOW
                )

    access = await _requests(store, clock, **settings).access(user_id)

    assert access.access is expected
    assert not access.can_suggest


async def test_three_open_requests_wait_for_the_owner_before_a_fourth() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)
    sent = [await _submit(service) for _ in range(3)]
    assert [result.outcome for result in sent] == [SubmitOutcome.CREATED] * 3

    fourth = await _submit(service)

    assert fourth.outcome is SubmitOutcome.REFUSED
    assert fourth.access.access is RequestAccess.TOO_MANY_OPEN
    assert fourth.access.can_suggest  # a limit, not a lost right

    # The owner closes one: the next goes through.
    first: DailyContentRequest = sent[0].request
    async with store() as uow:
        await uow.daily_requests.update(
            first.id,
            expected_version=first.version,
            at=NOW,
            status=DailyRequestStatus.COMPLETED,
            admin_note=UNSET,
        )
    assert (await _submit(service)).outcome is SubmitOutcome.CREATED


async def test_five_requests_a_day_then_none_until_the_day_has_passed() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)
    for _ in range(5):
        result = await _submit(service)
        assert result.outcome is SubmitOutcome.CREATED
        async with store() as uow:  # closed at once: only the daily limit counts
            await uow.daily_requests.update(
                result.request.id,
                expected_version=1,
                at=clock.now,
                status=DailyRequestStatus.REJECTED,
                admin_note=UNSET,
            )
        clock.now += timedelta(hours=1)

    limited = await _submit(service)
    assert limited.outcome is SubmitOutcome.REFUSED
    assert limited.access.access is RequestAccess.RATE_LIMITED

    # The first of the five leaves the window: a day is 24 hours, sliding.
    clock.now = NOW + timedelta(hours=24, seconds=1)
    assert (await _submit(service)).outcome is SubmitOutcome.CREATED


# --------------------------------------------------------------------------- #
# Sending                                                                     #
# --------------------------------------------------------------------------- #


async def test_a_draft_becomes_one_request_however_often_it_is_sent() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)
    draft = uuid4()

    first = await _submit(service, _text(5, "more cats"), _photo(6), draft=draft)
    again = await _submit(service, _text(5, "more cats"), _photo(6), draft=draft)

    assert first.outcome is SubmitOutcome.CREATED
    assert again.outcome is SubmitOutcome.ALREADY_SUBMITTED
    assert again.request.id == first.request.id
    requests = store.unit.daily_requests
    assert len(requests.requests) == 1
    request = first.request
    assert request.status is DailyRequestStatus.NEW
    assert request.purchases_at_submit == 20
    assert request.created_at == NOW
    items = await requests.items_of(request.id)
    assert [(item.position, item.kind) for item in items] == [
        (1, DailyContentKind.TEXT),
        (2, DailyContentKind.PHOTO),
    ]
    assert items[1].file_id == "photo-6"
    (event,) = await requests.events_of(request.id)
    assert (event.kind, event.actor, event.to_status) == (
        DailyRequestEventKind.SUBMITTED,
        f"user:{BUYER}",
        DailyRequestStatus.NEW,
    )
    assert await service.find_submitted(BUYER, draft) == request


async def test_a_draft_sent_again_is_recognised_even_at_the_limit() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)
    for _ in range(2):
        await _submit(service)
    draft = uuid4()
    third = await _submit(service, draft=draft)  # now three are open

    again = await _submit(service, draft=draft)

    assert again.outcome is SubmitOutcome.ALREADY_SUBMITTED
    assert again.request.id == third.request.id


async def test_a_draft_must_hold_one_to_ten_messages_of_at_most_2000_characters() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)

    empty = await service.submit(BUYER, client_draft_id=uuid4(), items=())
    eleven = await _submit(service, *(_text(n) for n in range(REQUEST_MAX_ITEMS + 1)))
    too_long = await _submit(service, _text(1, "x" * (REQUEST_TEXT_LIMIT + 1)))
    longest = await _submit(service, _text(1, "x" * REQUEST_TEXT_LIMIT))
    ten = await _submit(service, *(_text(n) for n in range(REQUEST_MAX_ITEMS)))

    assert empty.outcome is SubmitOutcome.EMPTY
    assert eleven.outcome is SubmitOutcome.INVALID
    assert too_long.outcome is SubmitOutcome.INVALID
    assert longest.outcome is SubmitOutcome.CREATED
    assert ten.outcome is SubmitOutcome.CREATED
    assert len(store.unit.daily_requests.requests) == 2


async def test_a_send_already_under_way_turns_the_second_away() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    locks = FakeLockManager()
    locks.busy_keys.add(submit_lock_key(BUYER))

    result = await _submit(_requests(store, clock, locks))

    assert result.outcome is SubmitOutcome.BUSY
    assert store.unit.daily_requests.requests == {}


async def test_the_send_lock_is_taken_per_buyer_and_released() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    locks = FakeLockManager()

    await _submit(_requests(store, clock, locks))

    assert [key for key, _ in locks.acquired] == [submit_lock_key(BUYER)]
    assert locks.held == {}


async def test_another_buyers_draft_is_not_theirs() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    await _subscriber(store, OTHER)
    service = _requests(store, clock)
    draft = uuid4()
    await _submit(service, draft=draft)

    stolen = await _submit(service, user_id=OTHER, draft=draft)

    assert stolen.outcome is SubmitOutcome.INVALID
    assert await service.find_submitted(OTHER, draft) is None
    assert len(store.unit.daily_requests.requests) == 1


async def test_access_is_checked_again_when_the_draft_is_sent() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    service = _requests(store, clock)
    assert (await service.access(BUYER)).allowed  # the draft was opened…
    async with store() as uow:  # …then the buyer was banned
        await uow.daily_subscriptions.set_requests_ban(BUYER, banned_by="admin:root", at=NOW)

    result = await _submit(service)

    assert result.outcome is SubmitOutcome.REFUSED
    assert result.access.access is RequestAccess.BANNED
    assert store.unit.daily_requests.requests == {}


# --------------------------------------------------------------------------- #
# The outbox: answers and "send it to me"                                     #
# --------------------------------------------------------------------------- #


@dataclass
class RecordingTextSender:
    sent: list[tuple[int, str]] = field(default_factory=list)
    failures: list[DailySendError] = field(default_factory=list)

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text))
        return 500 + len(self.sent)


@dataclass
class RecordingItemSender:
    sent: list[tuple[int, DailyContent, bool]] = field(default_factory=list)

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        self.sent.append((chat_id, content, protect_content))
        return 700 + len(self.sent)


def _outbox(
    store: FakeUnitOfWorkFactory,
    clock: Clock,
    text: RecordingTextSender,
    items: RecordingItemSender | None = None,
    **settings: Any,
) -> DailyOutboxService:
    return DailyOutboxService(
        uow_factory=store,
        settings=_settings(**settings),
        senders={DailyOutboxChannel.DAILY: text},
        item_sender=items,
        clock=clock,
        jitter=lambda: 0.0,
    )


def _admin(store: FakeUnitOfWorkFactory, clock: Clock, **settings: Any) -> DailyAdminService:
    daily = _settings(**settings)
    return DailyAdminService(
        uow_factory=store,
        settings=daily,
        library=DailyLibraryService(uow_factory=store, settings=daily, clock=clock),
        clock=clock,
    )


async def _answered(store: FakeUnitOfWorkFactory, clock: Clock) -> int:
    """A buyer's request with one answer queued; its id."""
    await _subscriber(store)
    sent = await _submit(_requests(store, clock), _text(1))
    request_id: int = sent.request.id
    await _admin(store, clock).reply("root", request_id, text="Thanks <3", client_key=uuid4())
    return request_id


async def _outcomes(store: FakeUnitOfWorkFactory, request_id: int) -> list[Any]:
    events = await store.unit.daily_requests.events_of(request_id)
    return [
        (event.kind, event.actor, event.body)
        for event in events
        if event.kind in (DailyRequestEventKind.REPLY_SENT, DailyRequestEventKind.REPLY_FAILED)
    ]


async def test_an_answer_that_reached_the_buyer_is_recorded_as_sent() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    request_id = await _answered(store, clock)
    text = RecordingTextSender()

    (report,) = await _outbox(store, clock, text).process_due()

    assert report.step is OutboxStep.SENT
    assert text.sent == [
        (BUYER, f"💬 Ответ на ваше пожелание #{request_id}:\n\nThanks &lt;3"),
    ]
    assert await _outcomes(store, request_id) == [
        (DailyRequestEventKind.REPLY_SENT, "system", None),
    ]


async def test_an_answer_to_a_buyer_who_blocked_the_bot_is_recorded_as_failed() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    request_id = await _answered(store, clock)
    blocked = RecordingTextSender(failures=[DailySendBlockedError("Forbidden: bot was blocked")])

    (report,) = await _outbox(store, clock, blocked).process_due()

    assert report.step is OutboxStep.FAILED
    assert await _outcomes(store, request_id) == [
        (DailyRequestEventKind.REPLY_FAILED, "system", "Forbidden: bot was blocked"),
    ]


async def test_an_uncertain_failure_records_nothing_until_it_is_decided() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    request_id = await _answered(store, clock)
    flaky = RecordingTextSender(failures=[DailySendTransientError("Telegram is unavailable")])
    outbox = _outbox(store, clock, flaky)

    (first,) = await outbox.process_due()
    assert first.step is OutboxStep.RETRY_SCHEDULED
    assert await _outcomes(store, request_id) == []

    clock.now += timedelta(hours=1)
    (second,) = await outbox.process_due()
    assert second.step is OutboxStep.SENT
    assert [kind for kind, _, _ in await _outcomes(store, request_id)] == [
        DailyRequestEventKind.REPLY_SENT
    ]


async def test_an_answer_dropped_at_its_deadline_is_recorded_as_failed() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    request_id = await _answered(store, clock)
    outbox = _outbox(store, clock, RecordingTextSender())
    clock.now = NOW + REQUEST_REPLY_TTL

    assert await outbox.expire_overdue() == 1
    assert await outbox.expire_overdue() == 0

    ((kind, actor, body),) = await _outcomes(store, request_id)
    assert (kind, actor) == (DailyRequestEventKind.REPLY_FAILED, "system")
    assert body == "The deadline passed before the message could be sent"


async def test_other_messages_leave_no_trace_in_any_request() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    async with store() as uow:
        await uow.daily_outbox.enqueue(
            DailyOutboxDraft(
                kind=DailyOutboxKind.ALERT,
                channel=DailyOutboxChannel.DAILY,
                chat_id=OWNER,
                body="alert",
                dedupe_key="alert",
                first_attempt_at=NOW,
                deadline_at=NOW + timedelta(hours=1),
            )
        )

    (report,) = await _outbox(store, clock, RecordingTextSender()).process_due()

    assert report.step is OutboxStep.SENT
    assert store.unit.daily_requests.events == {}


async def test_a_library_item_goes_to_the_owner_as_buyers_get_it() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    async with store() as uow:
        item, _ = await uow.daily_content.add(_photo(3, user_id=OWNER))
    admin = _admin(store, clock, admin_telegram_ids=(OWNER, SECOND_OWNER))
    queued = await admin.send_library_item("root", item.id, uuid4())
    text, items = RecordingTextSender(), RecordingItemSender()

    reports = await _outbox(
        store,
        clock,
        text,
        items,
        admin_telegram_ids=(OWNER, SECOND_OWNER),
        protect_content=True,
    ).process_due()

    assert [report.step for report in reports] == [OutboxStep.SENT, OutboxStep.SENT]
    assert [message.kind for message in queued.messages] == [DailyOutboxKind.LIBRARY_PREVIEW] * 2
    assert [(chat, content.id, protect) for chat, content, protect in items.sent] == [
        (OWNER, item.id, True),
        (SECOND_OWNER, item.id, True),
    ]
    assert text.sent == []  # the label is never sent


async def test_a_buyers_attachment_goes_to_the_owner_forwardable() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    await _subscriber(store)
    sent = await _submit(_requests(store, clock), _text(1), _photo(2))
    photo = (await store.unit.daily_requests.items_of(sent.request.id))[1]
    await _admin(store, clock).send_request_item("root", sent.request.id, photo.id, uuid4())
    items = RecordingItemSender()

    (report,) = await _outbox(
        store, clock, RecordingTextSender(), items, protect_content=True
    ).process_due()

    assert report.step is OutboxStep.SENT
    ((chat_id, content, protect),) = items.sent
    assert (chat_id, protect) == (OWNER, False)  # the owner may pass it on to the library
    assert (content.kind, content.file_id, content.body) == (
        DailyContentKind.PHOTO,
        "photo-2",
        "look at this",
    )
    assert (content.source_chat_id, content.source_message_id) == (BUYER, 2)


async def test_an_item_with_no_daily_bot_to_send_it_fails_at_once() -> None:
    store, clock = FakeUnitOfWorkFactory(), Clock()
    async with store() as uow:
        item, _ = await uow.daily_content.add(_photo(3, user_id=OWNER))
    await _admin(store, clock).send_library_item("root", item.id, uuid4())

    (report,) = await _outbox(store, clock, RecordingTextSender(), None).process_due()

    assert report.step is OutboxStep.FAILED
    (message,) = store.unit.daily_outbox.items.values()
    assert message.status is DailyOutboxStatus.FAILED
    assert message.last_error == "Only the Daily bot sends items"


# --------------------------------------------------------------------------- #
# PostgreSQL — two sends of one buyer at once                                 #
# --------------------------------------------------------------------------- #


class _NoLocks:
    """A lock manager that never refuses — as if the Redis lock had run out."""

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        wait_seconds: float = 0.0,
    ) -> AsyncIterator[None]:
        del key, ttl_seconds, wait_seconds
        yield


async def test_two_drafts_sent_at_once_cannot_pass_the_open_limit_together(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The subscription's row queues one buyer's sends even without Redis."""
    await seed_buyer(live_uow_factory, BUYER, completed=20)
    async with live_uow_factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=NOW)
        await uow.daily_subscriptions.activate(BUYER, at=NOW, anchor_minute=720)
    service = DailyRequestService(
        uow_factory=live_uow_factory,
        settings=_settings(),
        locks=_NoLocks(),
        clock=Clock(),
    )
    for _ in range(2):  # two of the three open already
        assert (await _submit(service)).outcome is SubmitOutcome.CREATED
    await warm_pool(live_uow_factory, 2)
    gate = Gate(2)
    racing = DailyRequestService(
        uow_factory=gated(live_uow_factory, gate, repository="daily_requests", method="count_open"),
        settings=_settings(),
        locks=_NoLocks(),
        clock=Clock(),
    )

    results = await all_at_once(_submit(racing), _submit(racing))

    assert not [result for result in results if isinstance(result, BaseException)]
    assert sorted(result.outcome for result in results) == [
        SubmitOutcome.CREATED,
        SubmitOutcome.REFUSED,
    ]
    async with live_uow_factory() as uow:
        assert await uow.daily_requests.count_open(BUYER) == 3
