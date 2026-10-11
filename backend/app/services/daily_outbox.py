"""Daily Content: the outbox sender — alerts and notices, reliably.

Every message the runtime owes somebody, other than a library item, is
queued in the outbox — once per ``dedupe_key`` — in the same transaction as
the decision to send it. Sending follows the deliveries' rules:

1. **claim** — lease the due messages (``SKIP LOCKED``: two workers split them);
2. **send** — outside any transaction, through the bot of the message's
   channel: the Daily bot, or the shop bot for what the Daily bot cannot say;
3. **record** — against the lease's token.

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
from app.domain.daily import retry_delay

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from app.core.config import DailySettings
    from app.domain.daily import DailyTextSender
    from app.domain.entities import DailyOutboxMessage
    from app.domain.enums import DailyOutboxChannel
    from app.domain.uow import UnitOfWorkFactory

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


@dataclass(frozen=True, slots=True)
class DailyOutboxService:
    """Sends the outbox's messages through the bot of their channel."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    senders: Mapping[DailyOutboxChannel, DailyTextSender]
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
        """Drop the messages whose deadline passed unsent."""
        if not self.settings.enabled:
            return 0
        async with self.uow_factory() as uow:
            expired = await uow.daily_outbox.expire_overdue(now=self.clock(), limit=limit)
        if expired:
            logger.info("daily_outbox_expired", count=expired)
        return expired

    async def _send(self, message: DailyOutboxMessage) -> OutboxReport:
        if message.lease_until is not None and self.clock() >= message.lease_until:
            # Earlier messages of the batch took too long: another worker may
            # own this one by now. Never send on an expired lease.
            return await self._retry(message, error="The lease ran out before sending")
        sender = self.senders.get(message.channel)
        if sender is None:
            return await self._fail(message, error=f"No bot sends the {message.channel} channel")
        try:
            telegram_message_id = await sender.send_text(message.chat_id, message.body)
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

    async def _record_sent(
        self, message: DailyOutboxMessage, telegram_message_id: int
    ) -> OutboxReport:
        token = _token(message)
        async with self.uow_factory() as uow:
            done = await uow.daily_outbox.finish_sent(
                token,
                at=self.clock(),
                telegram_message_id=telegram_message_id,
            )
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
        async with self.uow_factory() as uow:
            done = await uow.daily_outbox.finish_failed(
                _token(message), at=self.clock(), error=error
            )
        if done is None:
            return self._lost(message)
        logger.warning("daily_outbox_failed", message_id=message.id, error=error)
        return OutboxReport(message_id=message.id, step=OutboxStep.FAILED)

    @staticmethod
    def _lost(message: DailyOutboxMessage) -> OutboxReport:
        logger.warning("daily_outbox_lease_lost", message_id=message.id)
        return OutboxReport(message_id=message.id, step=OutboxStep.LEASE_LOST)


def _token(message: DailyOutboxMessage) -> UUID:
    if message.lease_token is None:  # pragma: no cover — claimed messages hold a lease
        message_text = "A claimed outbox message holds no lease"
        raise RuntimeError(message_text)
    return message.lease_token


def _seconds(hint: float | None) -> timedelta | None:
    return timedelta(seconds=hint) if hint else None


__all__ = ["DailyOutboxService", "OutboxReport", "OutboxStep"]
