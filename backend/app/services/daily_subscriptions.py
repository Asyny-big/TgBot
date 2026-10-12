"""Daily Content: a buyer's side of the bot — first contact, activation, pause.

**First contact** is any message the buyer sends. Unless Daily Content is off,
the buyer is outside a beta, or the buyer never used the shop, the moment is
recorded. Whether it also *activates* depends on one thing only: the loyalty
programme's own count of completed purchases, read through the shared
predicate. Below 15 the buyer is told how far they are and nothing else
happens — an early contact never becomes the anchor. From 15 the first contact
activates, in one transaction:

* the subscription becomes active, anchored at the minute of *this* contact
  (Europe/Moscow): the buyer's daily window runs an hour either side of it;
* the activation delivery is opened — that day's delivery.

The first item is sent after the commit, by the delivery engine, under its
lease: a double ``/start``, a replayed webhook or parallel updates find the row
locked, then active, and send nothing more.

Nothing here writes while Daily Content is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.domain.commands import DailyDeliveryDraft
from app.domain.daily import activation_window, anchor_minute_of, has_daily_access
from app.domain.enums import DailyDeliveryTrigger, DailySubscriptionStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.entities import DailyDelivery, DailySubscription
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ContactOutcome(StrEnum):
    """What a buyer's message meant for their subscription."""

    DISABLED = "disabled"
    """Daily Content is switched off: nothing was recorded."""

    NOT_INVITED = "not_invited"
    """A beta is running and the buyer is not in it: nothing was recorded."""

    OWNER = "owner"
    """One of the owner's accounts: never a subscriber, nothing was recorded."""

    UNKNOWN_BUYER = "unknown_buyer"
    """Never used the shop, so no purchases: nothing was recorded."""

    NO_ACCESS = "no_access"
    """Fewer than 15 completed purchases: the contact was recorded, no anchor."""

    ACTIVATED = "activated"
    """This contact activated the subscription; the first item is due now."""

    ACTIVE = "active"
    """Already active: nothing new happens."""

    PAUSED = "paused"
    """Activated and paused."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactResult:
    """The outcome of one contact, and what the caller needs to act on it."""

    outcome: ContactOutcome
    completed_purchases: int = 0
    subscription: DailySubscription | None = None
    delivery: DailyDelivery | None = None
    """The activation delivery, when this contact activated."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyStatus:
    """What "📊 My status" shows."""

    subscription: DailySubscription
    received: int
    available: int


@dataclass(frozen=True, slots=True)
class DailySubscriptionService:
    """A buyer's relationship with the Daily Content bot."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    # Injected so tests can pin the moment of a contact — the anchor is its minute.
    clock: Callable[[], datetime] = field(default=_utc_now)

    @property
    def enabled(self) -> bool:
        """Whether Daily Content is switched on (``DAILY_ENABLED``)."""
        return self.settings.enabled

    def is_owner(self, user_id: int) -> bool:
        """Whether this account is one of the owner's (``DAILY_ADMIN_TELEGRAM_IDS``)."""
        return user_id in self.settings.admin_telegram_ids

    def _invited(self, user_id: int) -> bool:
        beta = self.settings.beta_user_ids
        return not beta or user_id in beta

    def _turned_away(self, user_id: int) -> ContactOutcome | None:
        """Why nothing is recorded for this account at all; ``None`` when it may go on."""
        if not self.enabled:
            return ContactOutcome.DISABLED
        if self.is_owner(user_id):
            return ContactOutcome.OWNER
        if not self._invited(user_id):
            return ContactOutcome.NOT_INVITED
        return None

    async def first_contact(self, user_id: int) -> ContactResult:
        """Record a buyer's message, and activate them if they have access now.

        Idempotent: once active, a buyer's messages change nothing. The row is
        locked from the first read, so concurrent messages queue behind it.
        """
        turned_away = self._turned_away(user_id)
        if turned_away is not None:
            return ContactResult(outcome=turned_away)
        at = self.clock()
        async with self.uow_factory() as uow:
            if await uow.users.get(user_id) is None:
                return ContactResult(outcome=ContactOutcome.UNKNOWN_BUYER)
            subscription = await uow.daily_subscriptions.record_first_interaction(user_id, at=at)
            if subscription.is_blocked:
                # Writing to the bot is proof enough that it is unblocked.
                unblocked = await uow.daily_subscriptions.mark_unblocked(user_id)
                subscription = unblocked if unblocked is not None else subscription
            if subscription.status is not DailySubscriptionStatus.INACTIVE:
                return ContactResult(
                    outcome=_ACTIVATED_OUTCOMES[subscription.status],
                    subscription=subscription,
                )

            completed = await uow.loyalty.count_completed(user_id)
            if not has_daily_access(completed):
                return ContactResult(
                    outcome=ContactOutcome.NO_ACCESS,
                    completed_purchases=completed,
                    subscription=subscription,
                )

            anchor = anchor_minute_of(at)
            subscription = await uow.daily_subscriptions.activate(
                user_id,
                at=at,
                anchor_minute=anchor,
            )
            delivery = await uow.daily_deliveries.open(
                DailyDeliveryDraft(
                    user_id=user_id,
                    trigger=DailyDeliveryTrigger.ACTIVATION,
                    window=activation_window(at, recovery=self.settings.recovery),
                    first_attempt_at=at,
                )
            )
        logger.info(
            "daily_subscription_activated",
            user_id=user_id,
            anchor_minute=anchor,
            completed=completed,
            delivery_id=delivery.id if delivery is not None else None,
        )
        return ContactResult(
            outcome=ContactOutcome.ACTIVATED,
            completed_purchases=completed,
            subscription=subscription,
            delivery=delivery,
        )

    async def pause(self, user_id: int) -> DailySubscription | None:
        """Stop deliveries for now. ``None`` when the buyer was never activated.

        Open deliveries are left alone: one still due when the buyer resumes
        goes out as planned, one due while paused is cancelled by the engine.
        """
        return await self._change(user_id, pause=True)

    async def resume(self, user_id: int) -> DailySubscription | None:
        """Deliver again. ``None`` when the buyer was never activated."""
        return await self._change(user_id, pause=False)

    async def status(self, user_id: int) -> DailyStatus | None:
        """What the buyer received and has left; ``None`` when never activated."""
        if not self.enabled:
            return None
        async with self.uow_factory() as uow:
            subscription = await uow.daily_subscriptions.get(user_id)
            if subscription is None or subscription.status is DailySubscriptionStatus.INACTIVE:
                return None
            received = await uow.daily_deliveries.count_sent(user_id)
            available = await uow.daily_content.count_available_for(user_id)
        return DailyStatus(subscription=subscription, received=received, available=available)

    async def blocked(self, user_id: int) -> None:
        """The buyer blocked the bot. Nothing to do for somebody never recorded."""
        if not self.enabled:
            return
        async with self.uow_factory() as uow:
            changed = await uow.daily_subscriptions.mark_blocked(user_id, at=self.clock())
        if changed is not None:
            logger.info("daily_bot_blocked", user_id=user_id)

    async def unblocked(self, user_id: int) -> None:
        """The buyer unblocked the bot: deliveries resume, quietly."""
        if not self.enabled:
            return
        async with self.uow_factory() as uow:
            changed = await uow.daily_subscriptions.mark_unblocked(user_id)
        if changed is not None:
            logger.info("daily_bot_unblocked", user_id=user_id)

    async def _change(self, user_id: int, *, pause: bool) -> DailySubscription | None:
        if not self.enabled:
            return None
        at = self.clock()
        async with self.uow_factory() as uow:
            repository = uow.daily_subscriptions
            try:
                changed = (
                    await repository.pause(user_id, at=at)
                    if pause
                    else await repository.resume(user_id, at=at)
                )
            except ConflictError:
                return None
        logger.info("daily_subscription_changed", user_id=user_id, paused=pause)
        return changed


_ACTIVATED_OUTCOMES = {
    DailySubscriptionStatus.ACTIVE: ContactOutcome.ACTIVE,
    DailySubscriptionStatus.PAUSED: ContactOutcome.PAUSED,
}


__all__ = [
    "ContactOutcome",
    "ContactResult",
    "DailyStatus",
    "DailySubscriptionService",
]
