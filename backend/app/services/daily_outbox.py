"""Daily Content: the outbox sender — alerts, notices, answers, reliably.

Every message the runtime owes somebody, other than a delivery, is queued in
the outbox — once per ``dedupe_key`` — in the same transaction as the decision
to send it: alerts, notices, the owner's answers to content requests, and the
items the admin panel sends to the owner's own Telegram ("send it to me").
Sending follows the deliveries' rules:

1. **claim** — lease the due messages (``SKIP LOCKED``: two workers split them);
2. **send** — outside any transaction, through the bot of the message's
   channel: the Daily bot, or the shop bot for what the Daily bot cannot say.
   A message carrying an item (a library item, a request's attachment) is that
   item, sent by the Daily bot — the only bot whose file ids they are;
3. **record** — against the lease's token. The fate of an answer to a content
   request is written into the request's history in the same transaction:
   ``reply_sent``, or ``reply_failed`` when Telegram refused it for good or its
   deadline passed.

* accepted → sent;
* uncertain (network, flood control) → the same message again later, with
  back-off, until its attempts or its deadline run out;
* definitely refused (blocked, bad request) → failed: retrying cannot help.

A process that dies mid-send leaves its lease behind; once it runs out the
message is sent again. A notice may thus arrive twice — never zero times
before its deadline.

Nothing is sent while Daily Content is off.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from app.core.exceptions import DailySendError, DailySendTransientError
from app.core.logging import get_logger
from app.domain.commands import DailyRequestEventDraft
from app.domain.daily import retry_delay
from app.domain.daily_requests import SYSTEM_ACTOR, attachment_as_content, reply_outcome_key
from app.domain.enums import DailyOutboxChannel, DailyOutboxKind, DailyRequestEventKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from uuid import UUID

    from app.core.config import DailySettings
    from app.domain.daily import DailyContentSender, DailyTextSender
    from app.domain.entities import DailyContent, DailyOutboxMessage
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OutboxStep(StrEnum):
    """What one pass over one message did."""

    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"
    """Reported after another worker had taken the message over."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboxReport:
    """What one pass over one message did."""

    message_id: int
    step: OutboxStep
    retry_after: float | None = None


class _UndeliverableError(Exception):
    """Nothing can send this message: it fails at once, without an attempt."""


@dataclass(frozen=True, slots=True)
class DailyOutboxService:
    """Sends the outbox's messages through the bot of their channel."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    senders: Mapping[DailyOutboxChannel, DailyTextSender]
    item_sender: DailyContentSender | None = None
    """Sends the messages that carry an item — the Daily bot's, paced like the rest."""

    clock: Callable[[], datetime] = field(default=_utc_now)
    jitter: Callable[[], float] = field(default=random.random)

    async def process_due(self, *, limit: int = 10) -> tuple[OutboxReport, ...]:
        """Claim the messages due now, then send them one by one."""
        if not self.settings.enabled:
            return ()
        now = self.clock()
        async with self.uow_factory() as uow:
            claimed = await uow.daily_outbox.claim_due(
                now=now,
                limit=limit,
                lease_until=now + self.settings.lease,
            )
        return tuple([await self._send(message) for message in claimed])

    async def expire_overdue(self, *, limit: int = 100) -> int:
        """Drop the messages whose deadline passed unsent.

        An answer to a content request dropped this way is recorded as failed
        in the request's history, in the same transaction.
        """
        if not self.settings.enabled:
            return 0
        now = self.clock()
        async with self.uow_factory() as uow:
            expired = await uow.daily_outbox.expire_overdue(now=now, limit=limit)
            for message in expired:
                await _record_reply_outcome(uow, message, sent=False, at=now)
        if expired:
            logger.info("daily_outbox_expired", count=len(expired))
        return len(expired)

    async def _send(self, message: DailyOutboxMessage) -> OutboxReport:
        if message.lease_until is not None and self.clock() >= message.lease_until:
            # Earlier messages of the batch took too long: another worker may
            # own this one by now. Never send on an expired lease.
            return await self._retry(message, error="The lease ran out before sending")
        try:
            send = await self._sending(message)
        except _UndeliverableError as error:
            return await self._fail(message, error=str(error))
        try:
            telegram_message_id = await send()
        except DailySendTransientError as error:
            if message.attempts >= self.settings.max_attempts:
                return await self._fail(message, error=str(error))
            hint = error.retry_after
            return await self._retry(
                message,
                error=str(error),
                delay=retry_delay(
                    message.attempts,
                    initial=self.settings.retry_initial,
                    maximum=self.settings.retry_max,
                    jitter=self.jitter(),
                    retry_after=_seconds(hint),
                ),
                retry_after=hint,
            )
        except DailySendError as error:
            return await self._fail(message, error=str(error))
        return await self._record_sent(message, telegram_message_id)

    async def _sending(self, message: DailyOutboxMessage) -> Callable[[], Awaitable[int]]:
        """How to send ``message``: its text, or the item it carries.

        Raises:
            _UndeliverableError: no bot can send it, or its item is gone.
        """
        if message.content_id is None and message.request_item_id is None:
            sender = self.senders.get(message.channel)
            if sender is None:
                error = f"No bot sends the {message.channel} channel"
                raise _UndeliverableError(error)
            return lambda: sender.send_text(message.chat_id, message.body)
        item_sender = self.item_sender
        if item_sender is None or message.channel is not DailyOutboxChannel.DAILY:
            error = "Only the Daily bot sends items"
            raise _UndeliverableError(error)
        content = await self._item_of(message)
        # A library item goes as buyers get it; a buyer's file stays forwardable,
        # so the owner can pass it on to the library.
        protect = message.kind is DailyOutboxKind.LIBRARY_PREVIEW and self.settings.protect_content
        return lambda: item_sender.send(message.chat_id, content, protect_content=protect)

    async def _item_of(self, message: DailyOutboxMessage) -> DailyContent:
        async with self.uow_factory() as uow:
            if message.content_id is not None:
                content = await uow.daily_content.get(message.content_id)
            else:
                item = (
                    await uow.daily_requests.get_item(message.request_item_id)
                    if message.request_item_id is not None
                    else None
                )
                content = attachment_as_content(item) if item is not None else None
        if content is None:  # pragma: no cover — the foreign keys keep it
            error = "The item to send is gone"
            raise _UndeliverableError(error)
        return content

    async def _record_sent(
        self, message: DailyOutboxMessage, telegram_message_id: int
    ) -> OutboxReport:
        token = _token(message)
        now = self.clock()
        async with self.uow_factory() as uow:
            done = await uow.daily_outbox.finish_sent(
                token,
                at=now,
                telegram_message_id=telegram_message_id,
            )
            if done is not None:
                await _record_reply_outcome(uow, done, sent=True, at=now)
        if done is None:
            return self._lost(message)
        logger.info("daily_outbox_sent", message_id=message.id, kind=message.kind.value)
        return OutboxReport(message_id=message.id, step=OutboxStep.SENT)

    async def _retry(
        self,
        message: DailyOutboxMessage,
        *,
        error: str,
        delay: timedelta | None = None,
        retry_after: float | None = None,
    ) -> OutboxReport:
        now = self.clock()
        async with self.uow_factory() as uow:
            done = await uow.daily_outbox.finish_retry(
                _token(message),
                at=now,
                next_attempt_at=now + delay if delay is not None else now,
                error=error,
            )
        if done is None:
            return self._lost(message)
        logger.warning("daily_outbox_retry", message_id=message.id, error=error)
        return OutboxReport(
            message_id=message.id,
            step=OutboxStep.RETRY_SCHEDULED,
            retry_after=retry_after,
        )

    async def _fail(self, message: DailyOutboxMessage, *, error: str) -> OutboxReport:
        now = self.clock()
        async with self.uow_factory() as uow:
            done = await uow.daily_outbox.finish_failed(_token(message), at=now, error=error)
            if done is not None:
                await _record_reply_outcome(uow, done, sent=False, at=now)
        if done is None:
            return self._lost(message)
        logger.warning("daily_outbox_failed", message_id=message.id, error=error)
        return OutboxReport(message_id=message.id, step=OutboxStep.FAILED)

    @staticmethod
    def _lost(message: DailyOutboxMessage) -> OutboxReport:
        logger.warning("daily_outbox_lease_lost", message_id=message.id)
        return OutboxReport(message_id=message.id, step=OutboxStep.LEASE_LOST)


async def _record_reply_outcome(
    uow: UnitOfWork,
    message: DailyOutboxMessage,
    *,
    sent: bool,
    at: datetime,
) -> None:
    """For an answer to a content request: ``reply_sent`` or ``reply_failed``, once."""
    if message.kind is not DailyOutboxKind.REQUEST_REPLY:
        return
    queued = await uow.daily_requests.reply_of(message.id)
    if queued is None:  # pragma: no cover — an answer is queued with its history line
        logger.warning("daily_reply_without_request", message_id=message.id)
        return
    await uow.daily_requests.add_event(
        DailyRequestEventDraft(
            request_id=queued.request_id,
            kind=DailyRequestEventKind.REPLY_SENT if sent else DailyRequestEventKind.REPLY_FAILED,
            actor=SYSTEM_ACTOR,
            body=None if sent else message.last_error,
            outbox_message_id=message.id,
            idempotency_key=reply_outcome_key(message.id),
        ),
        at=at,
    )


def _token(message: DailyOutboxMessage) -> UUID:
    if message.lease_token is None:  # pragma: no cover — claimed messages hold a lease
        message_text = "A claimed outbox message holds no lease"
        raise RuntimeError(message_text)
    return message.lease_token


def _seconds(hint: float | None) -> timedelta | None:
    return timedelta(seconds=hint) if hint else None


__all__ = ["DailyOutboxService", "OutboxReport", "OutboxStep"]
