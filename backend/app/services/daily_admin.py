"""Daily Content: the admin panel's side.

Reads go through ``uow.daily_reports``; every change goes through the rule it
belongs to, never around it:

* **the library** — ``DailyLibraryService.change_status_as_admin``: the owner's
  own lock, transitions and duplicate check, attributed
  ``admin:<username>:<action>``. The owner pressing a button in Telegram and an
  admin clicking in the panel queue up on the same row;
* **"send it to me"** — one outbox message per owner account, carrying the item;
  the Daily bot sends it, paced like everything else. The browser never sees a
  file, a file id or a token;
* **content requests** — optimistic: a change names the version it was read at
  and is refused (409) when the request moved since, so two tabs never
  overwrite each other silently. Every change is a line of the history;
* **answers** — idempotent per key: the panel sends the same key when it
  retries, and the answer is queued once — its outbox message and its history
  line in one transaction;
* **bans** — on the buyer's subscription row, attributed ``admin:<username>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.core.exceptions import (
    DailyContentDuplicateError,
    DailyContentNotFoundError,
    DailyDeliveryNotFoundError,
    DailyIdempotencyKeyReusedError,
    DailyLibraryActionError,
    DailyNoOwnerError,
    DailyReplyTooLongError,
    DailyRequestNotFoundError,
    DailyRequestVersionConflictError,
    DailySubscriberNotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.daily_bot import texts
from app.domain.commands import DailyOutboxDraft, DailyRequestEventDraft
from app.domain.daily import moscow_date_of
from app.domain.daily_reports import (
    DailyDeliveryDetail,
    DailyOverview,
    DailyReply,
    DailyRequestDetail,
    DailySubscriberDetail,
)
from app.domain.daily_requests import (
    REQUEST_NOTE_LIMIT,
    REQUEST_REPLY_LIMIT,
    REQUEST_REPLY_TTL,
    admin_actor,
    attachment_key,
    preview_key,
    reply_key,
)
from app.domain.enums import (
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyRequestEventKind,
)
from app.domain.patch import UNSET, Maybe, is_set
from app.services.daily_library import StatusOutcome

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from app.core.config import DailySettings
    from app.domain.daily import LibraryAction
    from app.domain.daily_reports import (
        DailyDeliveryEntry,
        DailyDeliveryFilters,
        DailyLibraryEntry,
        DailyLibraryFilters,
        DailyRequestEntry,
        DailyRequestFilters,
        DailySubscriberEntry,
        DailySubscriberFilters,
    )
    from app.domain.entities import DailyAlert, DailyOutboxMessage, DailyRequestEvent
    from app.domain.enums import DailyRequestStatus
    from app.domain.pagination import Page, PageRequest
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory
    from app.services.daily_library import DailyLibraryService

logger = get_logger(__name__)

TELEGRAM_TEXT_LIMIT: Final = 4096
SEND_TO_OWNER_TTL: Final = timedelta(hours=1)
"""A "send it to me" not sent within an hour is not wanted any more."""

_REPLY_OUTCOMES: Final = frozenset(
    {DailyRequestEventKind.REPLY_SENT, DailyRequestEventKind.REPLY_FAILED}
)

RECENT: Final = timedelta(hours=24)
"""How far back the overview counts lost outbox messages."""

DETAIL_ROWS: Final = 20
"""Latest deliveries and requests shown on a subscriber's page."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplyResult:
    """An answer, and whether this call queued it (``False``: a retry of it)."""

    reply: DailyReply
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SendResult:
    """The outbox messages taking an item to the owner's accounts."""

    messages: tuple[DailyOutboxMessage, ...]
    created: bool
    """``False``: the same click came back — nothing new was queued."""


@dataclass(frozen=True, slots=True)
class DailyAdminService:
    """What the admin panel reads and does about Daily Content."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    library: DailyLibraryService
    clock: Callable[[], datetime] = field(default=_utc_now)

    # ---------------------------------------------------------------- overview

    async def overview(self) -> DailyOverview:
        """Subscriptions, today's deliveries, the library, requests, alerts."""
        now = self.clock()
        today = moscow_date_of(now)
        async with self.uow_factory() as uow:
            counts = await uow.daily_reports.overview_counts(today=today, since=now - RECENT)
            library = await uow.daily_content.stats()
            alerts = await uow.daily_alerts.list_all()
        return DailyOverview(
            enabled=self.settings.enabled,
            today=today,
            counts=counts,
            library=library,
            alerts=alerts,
        )

    async def alerts(self) -> tuple[DailyAlert, ...]:
        """Every alert evaluated so far, firing or not."""
        async with self.uow_factory() as uow:
            return await uow.daily_alerts.list_all()

    # ----------------------------------------------------------------- library

    async def library_page(
        self,
        filters: DailyLibraryFilters,
        page: PageRequest,
    ) -> Page[DailyLibraryEntry]:
        async with self.uow_factory() as uow:
            return await uow.daily_reports.library(filters, page)

    async def library_entry(self, content_id: int) -> DailyLibraryEntry:
        """One item.

        Raises:
            DailyContentNotFoundError: no such item.
        """
        async with self.uow_factory() as uow:
            entry = await uow.daily_reports.library_entry(content_id)
        if entry is None:
            raise DailyContentNotFoundError(content_id=content_id)
        return entry

    async def change_library_status(
        self,
        username: str,
        content_id: int,
        action: LibraryAction,
    ) -> DailyLibraryEntry:
        """Disable, enable, archive or restore an item — exactly as the owner would.

        Pressing the same action twice changes nothing the second time.

        Raises:
            DailyContentNotFoundError: no such item.
            DailyLibraryActionError: the item's status does not allow it.
            DailyContentDuplicateError: restoring it would duplicate a live item.
        """
        result = await self.library.change_status_as_admin(username, content_id, action)
        match result.outcome:
            case StatusOutcome.NOT_FOUND | StatusOutcome.NOT_OWNER:
                raise DailyContentNotFoundError(content_id=content_id)
            case StatusOutcome.NOT_ALLOWED:
                status = result.item.status.value if result.item is not None else None
                raise DailyLibraryActionError(
                    content_id=content_id,
                    status=status,
                    action=action.value,
                )
            case StatusOutcome.DUPLICATE:
                twin_id = result.twin.id if result.twin is not None else content_id
                raise DailyContentDuplicateError(existing_id=twin_id, content_id=content_id)
            case StatusOutcome.CHANGED | StatusOutcome.UNCHANGED:
                return await self.library_entry(content_id)

    async def send_library_item(
        self,
        username: str,
        content_id: int,
        client_key: UUID,
    ) -> SendResult:
        """Queue the item to every owner account, as buyers get it.

        Raises:
            DailyNoOwnerError: no owner account is configured.
            DailyContentNotFoundError: no such item.
        """
        owners = self._owners()
        now = self.clock()
        async with self.uow_factory() as uow:
            if await uow.daily_content.get(content_id) is None:
                raise DailyContentNotFoundError(content_id=content_id)
            result = await _queue_for_owners(
                uow,
                [
                    DailyOutboxDraft(
                        kind=DailyOutboxKind.LIBRARY_PREVIEW,
                        channel=DailyOutboxChannel.DAILY,
                        chat_id=owner_id,
                        body=f"library #{content_id} for {admin_actor(username)}",
                        dedupe_key=preview_key(content_id, owner_id, client_key),
                        first_attempt_at=now,
                        deadline_at=now + SEND_TO_OWNER_TTL,
                        content_id=content_id,
                    )
                    for owner_id in owners
                ],
            )
        if result.created:
            logger.info(
                "daily_library_item_sent_to_owner",
                content_id=content_id,
                actor=admin_actor(username),
            )
        return result

    # ------------------------------------------------------------- subscribers

    async def subscribers(
        self,
        filters: DailySubscriberFilters,
        page: PageRequest,
    ) -> Page[DailySubscriberEntry]:
        async with self.uow_factory() as uow:
            return await uow.daily_reports.subscribers(filters, page)

    async def subscriber(self, user_id: int) -> DailySubscriberDetail:
        """A subscriber with their latest deliveries and requests.

        Raises:
            DailySubscriberNotFoundError: the buyer never wrote to the Daily bot.
        """
        async with self.uow_factory() as uow:
            entry = await uow.daily_reports.subscriber(user_id)
            if entry is None:
                raise DailySubscriberNotFoundError(user_id=user_id)
            deliveries = await uow.daily_reports.recent_deliveries(user_id, limit=DETAIL_ROWS)
            requests = await uow.daily_reports.requests_of(user_id, limit=DETAIL_ROWS)
        return DailySubscriberDetail(entry=entry, deliveries=deliveries, requests=requests)

    async def set_requests_ban(
        self,
        username: str,
        user_id: int,
        *,
        banned: bool,
    ) -> DailySubscriberDetail:
        """Ban the buyer from sending content requests, or lift the ban. Idempotent.

        A ban stops the next step of a draft and every send; requests already
        sent stay as they are.

        Raises:
            DailySubscriberNotFoundError: the buyer never wrote to the Daily bot.
        """
        async with self.uow_factory() as uow:
            updated = await uow.daily_subscriptions.set_requests_ban(
                user_id,
                banned_by=admin_actor(username) if banned else None,
                at=self.clock(),
            )
        if updated is None:
            raise DailySubscriberNotFoundError(user_id=user_id)
        logger.info(
            "daily_requests_ban_set",
            user_id=user_id,
            banned=banned,
            actor=admin_actor(username),
        )
        return await self.subscriber(user_id)

    # -------------------------------------------------------------- deliveries

    async def deliveries(
        self,
        filters: DailyDeliveryFilters,
        page: PageRequest,
    ) -> Page[DailyDeliveryEntry]:
        async with self.uow_factory() as uow:
            return await uow.daily_reports.deliveries(filters, page)

    async def delivery(self, delivery_id: int) -> DailyDeliveryDetail:
        """A delivery with its attempts and its item.

        Raises:
            DailyDeliveryNotFoundError: no such delivery.
        """
        async with self.uow_factory() as uow:
            entry = await uow.daily_reports.delivery(delivery_id)
            if entry is None:
                raise DailyDeliveryNotFoundError(delivery_id=delivery_id)
            attempts = await uow.daily_deliveries.attempts_of(delivery_id)
            content_id = entry.delivery.content_id
            item = await uow.daily_content.get(content_id) if content_id is not None else None
        return DailyDeliveryDetail(entry=entry, attempts=attempts, item=item)

    # ---------------------------------------------------------------- requests

    async def requests(
        self,
        filters: DailyRequestFilters,
        page: PageRequest,
    ) -> Page[DailyRequestEntry]:
        async with self.uow_factory() as uow:
            return await uow.daily_reports.requests(filters, page)

    async def request(self, request_id: int) -> DailyRequestDetail:
        """Everything about one request.

        Raises:
            DailyRequestNotFoundError: no such request.
        """
        async with self.uow_factory() as uow:
            return await _detail(uow, request_id)

    async def update_request(
        self,
        username: str,
        request_id: int,
        *,
        version: int,
        status: Maybe[DailyRequestStatus] = UNSET,
        admin_note: Maybe[str | None] = UNSET,
    ) -> DailyRequestDetail:
        """Change the status or the note of a request, as read at ``version``.

        A change that changes nothing keeps the version; an empty note clears it.

        Raises:
            DailyRequestNotFoundError: no such request.
            DailyRequestVersionConflictError: it moved since ``version`` was read.
            ValidationError: the note is too long.
        """
        note = _clean_note(admin_note)
        actor = admin_actor(username)
        now = self.clock()
        async with self.uow_factory() as uow:
            current = await uow.daily_requests.lock(request_id)
            if current is None:
                raise DailyRequestNotFoundError(request_id=request_id)
            if current.version != version:
                raise DailyRequestVersionConflictError(
                    request_id=request_id,
                    current_version=current.version,
                )
            new_status: Maybe[DailyRequestStatus] = (
                status if is_set(status) and status is not current.status else UNSET
            )
            new_note: Maybe[str | None] = (
                note if is_set(note) and note != current.admin_note else UNSET
            )
            if not is_set(new_status) and not is_set(new_note):
                return await _detail(uow, request_id)
            updated = await uow.daily_requests.update(
                request_id,
                expected_version=version,
                at=now,
                status=new_status,
                admin_note=new_note,
            )
            if updated is None:  # pragma: no cover — the row is locked and checked above
                raise DailyRequestVersionConflictError(
                    request_id=request_id,
                    current_version=current.version,
                )
            if is_set(new_status):
                await uow.daily_requests.add_event(
                    DailyRequestEventDraft(
                        request_id=request_id,
                        kind=DailyRequestEventKind.STATUS_CHANGED,
                        actor=actor,
                        from_status=current.status,
                        to_status=new_status,
                    ),
                    at=now,
                )
            if is_set(new_note):
                await uow.daily_requests.add_event(
                    DailyRequestEventDraft(
                        request_id=request_id,
                        kind=DailyRequestEventKind.NOTE_CHANGED,
                        actor=actor,
                        body=new_note,
                    ),
                    at=now,
                )
            detail = await _detail(uow, request_id)
        logger.info(
            "daily_request_updated",
            request_id=request_id,
            version=updated.version,
            status=updated.status.value,
            actor=actor,
        )
        return detail

    async def reply(
        self,
        username: str,
        request_id: int,
        *,
        text: str,
        client_key: UUID,
    ) -> ReplyResult:
        """Answer the buyer through the Daily bot — once per ``client_key``.

        Raises:
            DailyRequestNotFoundError: no such request.
            DailyIdempotencyKeyReusedError: the key came back with another text.
            DailyReplyTooLongError: the formatted answer exceeds one message.
            ValidationError: the text is empty or too long.
        """
        text = text.strip()
        if not text or len(text) > REQUEST_REPLY_LIMIT:
            problem = f"The reply must be 1 to {REQUEST_REPLY_LIMIT} characters"
            raise ValidationError(problem, field="text")
        body = texts.request_reply(request_id, text)
        if len(body) > TELEGRAM_TEXT_LIMIT:
            raise DailyReplyTooLongError(length=len(body), limit=TELEGRAM_TEXT_LIMIT)
        key = reply_key(request_id, client_key)
        now = self.clock()
        async with self.uow_factory() as uow:
            # Locked: the same answer sent twice at once is queued once.
            request = await uow.daily_requests.lock(request_id)
            if request is None:
                raise DailyRequestNotFoundError(request_id=request_id)
            queued = await uow.daily_requests.find_event(key)
            if queued is not None:
                if queued.body != text:
                    raise DailyIdempotencyKeyReusedError(request_id=request_id)
                return ReplyResult(reply=await _reply(uow, queued), created=False)
            message = await uow.daily_outbox.enqueue(
                DailyOutboxDraft(
                    kind=DailyOutboxKind.REQUEST_REPLY,
                    channel=DailyOutboxChannel.DAILY,
                    chat_id=request.user_id,
                    body=body,
                    dedupe_key=key,
                    first_attempt_at=now,
                    deadline_at=now + REQUEST_REPLY_TTL,
                )
            )
            if message is None:
                message = await uow.daily_outbox.find_by_key(key)
            if message is None:  # pragma: no cover — the key was just taken
                raise DailyIdempotencyKeyReusedError(request_id=request_id)
            event = await uow.daily_requests.add_event(
                DailyRequestEventDraft(
                    request_id=request_id,
                    kind=DailyRequestEventKind.REPLY_QUEUED,
                    actor=admin_actor(username),
                    body=text,
                    outbox_message_id=message.id,
                    idempotency_key=key,
                ),
                at=now,
            )
            if event is None:  # pragma: no cover — checked under the lock above
                raise DailyIdempotencyKeyReusedError(request_id=request_id)
        logger.info(
            "daily_request_reply_queued",
            request_id=request_id,
            message_id=message.id,
            actor=admin_actor(username),
        )
        return ReplyResult(reply=DailyReply(event=event, message=message), created=True)

    async def send_request_item(
        self,
        username: str,
        request_id: int,
        item_id: int,
        client_key: UUID,
    ) -> SendResult:
        """Queue a buyer's attachment to every owner account.

        Raises:
            DailyNoOwnerError: no owner account is configured.
            DailyRequestNotFoundError: no such request, or no such message in it.
        """
        owners = self._owners()
        now = self.clock()
        async with self.uow_factory() as uow:
            item = await uow.daily_requests.get_item(item_id)
            if item is None or item.request_id != request_id:
                raise DailyRequestNotFoundError(request_id=request_id, item_id=item_id)
            result = await _queue_for_owners(
                uow,
                [
                    DailyOutboxDraft(
                        kind=DailyOutboxKind.REQUEST_ATTACHMENT,
                        channel=DailyOutboxChannel.DAILY,
                        chat_id=owner_id,
                        body=f"request #{request_id} item #{item_id} for {admin_actor(username)}",
                        dedupe_key=attachment_key(item_id, owner_id, client_key),
                        first_attempt_at=now,
                        deadline_at=now + SEND_TO_OWNER_TTL,
                        request_item_id=item_id,
                    )
                    for owner_id in owners
                ],
            )
        if result.created:
            logger.info(
                "daily_request_item_sent_to_owner",
                request_id=request_id,
                item_id=item_id,
                actor=admin_actor(username),
            )
        return result

    def _owners(self) -> tuple[int, ...]:
        owners = self.settings.admin_telegram_ids
        if not owners:
            raise DailyNoOwnerError
        return owners


async def _queue_for_owners(uow: UnitOfWork, drafts: list[DailyOutboxDraft]) -> SendResult:
    """Queue each draft once; a key queued before gives back its message."""
    messages: list[DailyOutboxMessage] = []
    created = False
    for draft in drafts:
        message = await uow.daily_outbox.enqueue(draft)
        if message is None:
            message = await uow.daily_outbox.find_by_key(draft.dedupe_key)
        else:
            created = True
        if message is not None:
            messages.append(message)
    return SendResult(messages=tuple(messages), created=created)


async def _detail(uow: UnitOfWork, request_id: int) -> DailyRequestDetail:
    entry = await uow.daily_reports.request_entry(request_id)
    if entry is None:
        raise DailyRequestNotFoundError(request_id=request_id)
    events = await uow.daily_requests.events_of(request_id)
    replies = [await _reply(uow, event, events) for event in events if _is_reply(event)]
    subscription = await uow.daily_subscriptions.get(entry.request.user_id)
    return DailyRequestDetail(
        entry=entry,
        items=await uow.daily_requests.items_of(request_id),
        events=events,
        replies=tuple(replies),
        banned=subscription is not None and subscription.requests_banned_at is not None,
    )


def _is_reply(event: DailyRequestEvent) -> bool:
    return event.kind is DailyRequestEventKind.REPLY_QUEUED


async def _reply(
    uow: UnitOfWork,
    queued: DailyRequestEvent,
    events: tuple[DailyRequestEvent, ...] | None = None,
) -> DailyReply:
    """An answer: its history line, its outbox message, and its fate so far."""
    message_id = queued.outbox_message_id
    message = await uow.daily_outbox.get(message_id) if message_id is not None else None
    if events is None:
        events = await uow.daily_requests.events_of(queued.request_id)
    outcome = next(
        (
            event
            for event in events
            if event.outbox_message_id == message_id and event.kind in _REPLY_OUTCOMES
        ),
        None,
    )
    return DailyReply(event=queued, message=message, outcome=outcome)


def _clean_note(note: Maybe[str | None]) -> Maybe[str | None]:
    """Trimmed; empty means no note.

    Raises:
        ValidationError: longer than the note's limit.
    """
    if not is_set(note) or note is None:
        return note
    trimmed = note.strip()
    if len(trimmed) > REQUEST_NOTE_LIMIT:
        message = f"The note must be at most {REQUEST_NOTE_LIMIT} characters"
        raise ValidationError(message, field="admin_note")
    return trimmed or None


__all__ = ["DailyAdminService", "ReplyResult", "SendResult"]
