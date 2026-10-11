"""Daily Content: buyers' content requests — the bot's side.

A buyer with 20 or more completed purchases may suggest content: a draft of
up to ten messages, kept in the bot's FSM storage, sent as one request.

**Access is checked at every step** — when the draft is started, for every
message added to it, and in the very transaction that creates the request —
through the loyalty programme's own count of completed purchases. A refund
that takes the buyer under 20, or a ban, stops the next step.

**Sending is idempotent.** The draft's id travels with the "Send" button, so
the same draft sent twice — a double tap, a retry after a network failure — is
one request:

* a Redis lock per buyer keeps two sends from even racing (the second is told
  the first is under way);
* the buyer's subscription row, locked for the transaction, queues every send
  of theirs, so the limits — three open requests, five a day — count exactly;
* the unique ``client_draft_id`` is the last word.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from app.core.exceptions import LockBusyError
from app.core.logging import get_logger
from app.domain.commands import DailyRequestDraft
from app.domain.daily import can_suggest_content
from app.domain.daily_requests import (
    REQUEST_OPEN_LIMIT,
    REQUEST_SUBMIT_LIMIT,
    REQUEST_SUBMIT_WINDOW,
    RequestAccess,
    item_problem,
)
from app.domain.enums import DailySubscriptionStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractAsyncContextManager
    from uuid import UUID

    from app.core.config import DailySettings
    from app.domain.commands import DailyContentDraft
    from app.domain.entities import DailyContentRequest, DailySubscription
    from app.domain.locks import LockManager
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)

_SUBMIT_LOCK_TTL_SECONDS: Final = 30.0
"""Outlives any send; a process that dies holding it frees it this late."""

_DRAFT_LOCK_TTL_SECONDS: Final = 15.0
_DRAFT_LOCK_WAIT_SECONDS: Final = 10.0


_SUGGESTING: Final = frozenset(
    {RequestAccess.ALLOWED, RequestAccess.TOO_MANY_OPEN, RequestAccess.RATE_LIMITED}
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def submit_lock_key(user_id: int) -> str:
    """One send of a content request per buyer at a time."""
    return f"daily-request-submit:{user_id}"


def draft_lock_key(user_id: int) -> str:
    """One change of a buyer's draft at a time."""
    return f"daily-request-draft:{user_id}"


class SubmitOutcome(StrEnum):
    """What sending a draft did."""

    CREATED = "created"
    ALREADY_SUBMITTED = "already_submitted"
    """This draft was sent before: the request it became."""

    REFUSED = "refused"
    """The buyer may not send a request now — ``access`` says why."""

    EMPTY = "empty"
    INVALID = "invalid"
    """A message breaks the draft's limits, or the draft is not this buyer's."""

    BUSY = "busy"
    """Another send of this buyer's is under way."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessResult:
    """Whether the buyer may suggest content now, and what the answer rests on."""

    access: RequestAccess
    completed_purchases: int = 0
    subscription: DailySubscription | None = None
    """The buyer's subscription as it was read, when there is one."""

    @property
    def allowed(self) -> bool:
        return self.access is RequestAccess.ALLOWED

    @property
    def can_suggest(self) -> bool:
        """Whether "💡 Suggest content" is the buyer's: the limits pass by themselves."""
        return self.access in _SUGGESTING


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitResult:
    """The outcome of sending a draft."""

    outcome: SubmitOutcome
    request: DailyContentRequest | None = None
    access: AccessResult | None = None


@dataclass(frozen=True, slots=True)
class DailyRequestService:
    """Starts and sends buyers' content requests."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    locks: LockManager
    clock: Callable[[], datetime] = field(default=_utc_now)

    def draft_lock(self, user_id: int) -> AbstractAsyncContextManager[None]:
        """Hold while changing the buyer's draft: its messages arrive at once.

        A change waits for the one before it — an album is many messages in
        the same instant, and none of them may be lost.

        Raises:
            LockBusyError: the draft stayed busy for the whole wait.
        """
        return self.locks.lock(
            draft_lock_key(user_id),
            ttl_seconds=_DRAFT_LOCK_TTL_SECONDS,
            wait_seconds=_DRAFT_LOCK_WAIT_SECONDS,
        )

    async def access(self, user_id: int) -> AccessResult:
        """Whether the buyer may suggest content right now."""
        async with self.uow_factory() as uow:
            subscription = await uow.daily_subscriptions.get(user_id)
            return await self._access(uow, user_id, subscription)

    async def submit(
        self,
        user_id: int,
        *,
        client_draft_id: UUID,
        items: Sequence[DailyContentDraft],
    ) -> SubmitResult:
        """Turn the buyer's draft into a request — once, however often it is sent."""
        try:
            async with self.locks.lock(
                submit_lock_key(user_id),
                ttl_seconds=_SUBMIT_LOCK_TTL_SECONDS,
            ):
                return await self._submit(user_id, client_draft_id, tuple(items))
        except LockBusyError:
            logger.info("daily_request_submit_busy", user_id=user_id)
            return SubmitResult(outcome=SubmitOutcome.BUSY)

    async def find_submitted(
        self, user_id: int, client_draft_id: UUID
    ) -> DailyContentRequest | None:
        """The request this buyer's draft became, if it was sent."""
        async with self.uow_factory() as uow:
            request = await uow.daily_requests.find_by_draft(client_draft_id)
        return request if request is not None and request.user_id == user_id else None

    async def _submit(
        self,
        user_id: int,
        client_draft_id: UUID,
        items: tuple[DailyContentDraft, ...],
    ) -> SubmitResult:
        async with self.uow_factory() as uow:
            # Every send of this buyer's queues up here: the limits count exactly.
            subscription = await uow.daily_subscriptions.lock(user_id)
            existing = await uow.daily_requests.find_by_draft(client_draft_id)
            if existing is not None:
                if existing.user_id != user_id:
                    logger.warning("daily_request_foreign_draft", user_id=user_id)
                    return SubmitResult(outcome=SubmitOutcome.INVALID)
                return SubmitResult(outcome=SubmitOutcome.ALREADY_SUBMITTED, request=existing)
            access = await self._access(uow, user_id, subscription)
            if not access.allowed:
                return SubmitResult(outcome=SubmitOutcome.REFUSED, access=access)
            if not items:
                return SubmitResult(outcome=SubmitOutcome.EMPTY)
            if any(
                item_problem(item, items_so_far=position) is not None
                for position, item in enumerate(items)
            ):
                return SubmitResult(outcome=SubmitOutcome.INVALID)
            request, created = await uow.daily_requests.create(
                DailyRequestDraft(
                    user_id=user_id,
                    client_draft_id=client_draft_id,
                    purchases_at_submit=access.completed_purchases,
                    items=items,
                ),
                at=self.clock(),
            )
        if created:
            logger.info(
                "daily_request_submitted",
                user_id=user_id,
                request_id=request.id,
                items=len(items),
            )
        return SubmitResult(
            outcome=SubmitOutcome.CREATED if created else SubmitOutcome.ALREADY_SUBMITTED,
            request=request,
        )

    async def _access(
        self,
        uow: UnitOfWork,
        user_id: int,
        subscription: DailySubscription | None,
    ) -> AccessResult:
        refusal = self._turned_away(user_id, subscription)
        if refusal is not None:
            return AccessResult(access=refusal, subscription=subscription)
        completed = await uow.loyalty.count_completed(user_id)
        return AccessResult(
            access=await self._within_limits(uow, user_id, completed),
            completed_purchases=completed,
            subscription=subscription,
        )

    def _turned_away(
        self,
        user_id: int,
        subscription: DailySubscription | None,
    ) -> RequestAccess | None:
        """What refuses this account before anything is counted; ``None`` if nothing."""
        settings = self.settings
        if not settings.enabled or (
            settings.beta_user_ids and user_id not in settings.beta_user_ids
        ):
            return RequestAccess.DISABLED
        if user_id in settings.admin_telegram_ids:
            return RequestAccess.OWNER
        if subscription is None or subscription.status is DailySubscriptionStatus.INACTIVE:
            return RequestAccess.NOT_SUBSCRIBED
        if subscription.requests_banned_at is not None:
            return RequestAccess.BANNED
        return None

    async def _within_limits(self, uow: UnitOfWork, user_id: int, completed: int) -> RequestAccess:
        """The milestone, then the open requests, then the day's sends."""
        if not can_suggest_content(completed):
            return RequestAccess.NOT_ELIGIBLE
        if await uow.daily_requests.count_open(user_id) >= REQUEST_OPEN_LIMIT:
            return RequestAccess.TOO_MANY_OPEN
        submitted = await uow.daily_requests.count_since(
            user_id,
            self.clock() - REQUEST_SUBMIT_WINDOW,
        )
        if submitted >= REQUEST_SUBMIT_LIMIT:
            return RequestAccess.RATE_LIMITED
        return RequestAccess.ALLOWED


__all__ = [
    "AccessResult",
    "DailyRequestService",
    "SubmitOutcome",
    "SubmitResult",
    "draft_lock_key",
    "submit_lock_key",
]
