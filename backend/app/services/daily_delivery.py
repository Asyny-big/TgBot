"""Daily Content delivery engine.

Turns a delivery row into one item in front of one buyer, at most once a day,
without losing the day to a technical failure.

One pass over a delivery has three steps, and only the middle one talks to
Telegram — never inside a database transaction:

1. **claim** — in one transaction: lock the delivery, check the buyer may still
   receive (subscribed, not blocked, the loyalty programme's own count of
   completed purchases), choose the item — or keep the one the delivery
   started with — and take a lease (a random token and an expiry);
2. **send** — through the ``DailyContentSender`` port;
3. **record** — in a new transaction, against the lease token, what Telegram
   said.

How each answer is recorded follows one rule — *was the buyer definitely left
without the message?*

* accepted → sent; the day is done;
* uncertain (network, timeout, Telegram's servers, flood control) → the *same*
  item again later. A timeout may hide a message Telegram took, so a different
  item would risk two items in one day;
* definitely refused because of the item → the item is taken out of the
  library and given back, and another one goes out the same day;
* definitely refused otherwise → the item is given back, another one is tried;
* the bot is blocked → the day is closed, the buyer marked blocked, the item
  given back.

A process that dies mid-send leaves its lease behind. Once the lease runs out,
the next pass takes the delivery over and sends the same item again: a possible
duplicate, accepted so that the day is not lost — and recorded, because the dead
attempt is closed as ``CRASHED``.

Two workers never send the same delivery: claims skip rows another transaction
holds, a live lease refuses a second claim, and every outcome is written against
the token of the lease it belongs to.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.exceptions import (
    ConflictError,
    DailyContentRejectedError,
    DailySendBlockedError,
    DailySendRejectedError,
    DailySendTransientError,
)
from app.core.logging import get_logger
from app.domain.commands import DailyDeliveryDraft
from app.domain.daily import (
    DeliveryStep,
    activation_window,
    due_window,
    has_daily_access,
    retry_delay,
)
from app.domain.enums import (
    DailyAttemptOutcome,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.core.exceptions import DailySendError
    from app.domain.daily import DailyContentSender
    from app.domain.entities import (
        DailyContent,
        DailyDelivery,
        DailyDeliveryAttempt,
        DailySubscription,
    )
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryReport:
    """What one pass over one delivery did."""

    delivery_id: int
    step: DeliveryStep
    user_id: int | None = None
    content_id: int | None = None
    recovered: bool = False
    """The pass took the delivery over from an attempt that never reported back."""

    retry_after: float | None = None
    """Telegram's flood-control hint, for a worker to slow the whole bot down."""


@dataclass(frozen=True, slots=True)
class _Claim:
    delivery: DailyDelivery
    attempt: DailyDeliveryAttempt
    content: DailyContent
    lease_until: datetime
    recovered: bool


@dataclass(frozen=True, slots=True)
class DailyDeliveryService:
    """Opens Daily Content deliveries and sends them, once a day per buyer."""

    uow_factory: UnitOfWorkFactory
    sender: DailyContentSender
    settings: DailySettings
    # Injected so tests can drive time and back-off deterministically.
    clock: Callable[[], datetime] = field(default=_utc_now)
    jitter: Callable[[], float] = field(default=random.random)

    # ----------------------------------------------------------------- opening

    async def open_activation(self, user_id: int, *, at: datetime) -> DailyDelivery | None:
        """Open the item sent right away at activation — that day's delivery.

        ``None`` when the buyer already has a delivery for that date.
        """
        window = activation_window(at, recovery=self.settings.recovery)
        async with self.uow_factory() as uow:
            return await uow.daily_deliveries.open(
                DailyDeliveryDraft(
                    user_id=user_id,
                    trigger=DailyDeliveryTrigger.ACTIVATION,
                    window=window,
                    first_attempt_at=at,
                )
            )

    async def open_due(
        self,
        subscription: DailySubscription,
        *,
        now: datetime,
    ) -> DailyDelivery | None:
        """Open the buyer's delivery for the window open right now, if any.

        Due at the window's centre — the buyer's own time — or at once when the
        window is already past its centre. ``None`` when nothing is due or the
        buyer already has that day's delivery: one per day, whoever asks.
        """
        if subscription.anchor_minute is None or not subscription.receives_deliveries:
            return None
        window = due_window(subscription.anchor_minute, now, recovery=self.settings.recovery)
        if window is None:
            return None
        async with self.uow_factory() as uow:
            return await uow.daily_deliveries.open(
                DailyDeliveryDraft(
                    user_id=subscription.user_id,
                    trigger=DailyDeliveryTrigger.SCHEDULE,
                    window=window,
                    first_attempt_at=max(now, window.center),
                )
            )

    # -------------------------------------------------------------- attempting

    async def deliver(self, delivery_id: int) -> DeliveryReport:
        """Attempt one delivery now — how the activation item goes out.

        When Telegram refuses an item, another one is tried right away, so the
        buyer still gets something on their first day.
        """
        if not self.settings.enabled:
            return DeliveryReport(delivery_id=delivery_id, step=DeliveryStep.DISABLED)
        report = DeliveryReport(delivery_id=delivery_id, step=DeliveryStep.NOT_DUE)
        for _ in range(self.settings.max_attempts):
            claimed = await self._claim_by_id(delivery_id)
            if isinstance(claimed, DeliveryReport):
                return claimed
            report = await self._send(claimed)
            if report.step is not DeliveryStep.ITEM_RELEASED:
                return report
        return report

    async def process_due(self, *, limit: int = 20) -> tuple[DeliveryReport, ...]:
        """Claim the deliveries due now, then send them one by one.

        Rows another worker holds are skipped, not waited for: two workers
        running this at once split the work between them.
        """
        if not self.settings.enabled:
            return ()
        now = self.clock()
        claims: list[_Claim] = []
        reports: list[DeliveryReport] = []
        async with self.uow_factory() as uow:
            for delivery in await uow.daily_deliveries.lock_due(now=now, limit=limit):
                claimed = await self._claim(uow, delivery, now)
                if isinstance(claimed, DeliveryReport):
                    reports.append(claimed)
                else:
                    claims.append(claimed)
        reports.extend([await self._send(claim) for claim in claims])
        return tuple(reports)

    async def expire_overdue(self, *, limit: int = 100) -> int:
        """Close deliveries whose deadline passed; their items are given back."""
        async with self.uow_factory() as uow:
            expired = await uow.daily_deliveries.expire_overdue(now=self.clock(), limit=limit)
        if expired:
            logger.info("daily_deliveries_expired", count=expired)
        return expired

    # ------------------------------------------------------------------ claim

    async def _claim_by_id(self, delivery_id: int) -> _Claim | DeliveryReport:
        """Lock one delivery and claim it, if it may be attempted now."""
        now = self.clock()
        async with self.uow_factory() as uow:
            delivery = await uow.daily_deliveries.lock(delivery_id)
            if delivery is None:
                return DeliveryReport(delivery_id=delivery_id, step=DeliveryStep.NOT_DUE)
            if delivery.status is DailyDeliveryStatus.SENT:
                return self._report(delivery, DeliveryStep.ALREADY_SENT)
            if not _attemptable(delivery, now):
                return self._report(delivery, DeliveryStep.NOT_DUE)
            return await self._claim(uow, delivery, now)

    async def _claim(
        self,
        uow: UnitOfWork,
        delivery: DailyDelivery,
        now: datetime,
    ) -> _Claim | DeliveryReport:
        """Lease the delivery for one attempt, or report why there is none.

        The caller holds the delivery's row lock.
        """
        user_id = delivery.user_id
        if not await self._may_receive(uow, user_id):
            await uow.daily_deliveries.cancel(delivery.id, at=now)
            logger.info("daily_delivery_cancelled", delivery_id=delivery.id, user_id=user_id)
            return self._report(delivery, DeliveryStep.CANCELLED)

        recovered = delivery.status is DailyDeliveryStatus.SENDING
        if delivery.content_id is not None:
            # A retry or a takeover: the same item, whatever happened to it since.
            content = await uow.daily_content.get(delivery.content_id)
        else:
            content = await uow.daily_content.pick_for(user_id)
        if content is None:
            await uow.daily_deliveries.mark_no_content(
                delivery.id,
                at=now,
                recheck_at=now + self.settings.no_content_recheck,
            )
            logger.info("daily_delivery_no_content", delivery_id=delivery.id, user_id=user_id)
            return self._report(delivery, DeliveryStep.NO_CONTENT)

        lease_until = now + self.settings.lease
        try:
            attempt = await uow.daily_deliveries.start_attempt(
                delivery.id,
                content_id=content.id,
                now=now,
                lease_until=lease_until,
            )
        except ConflictError as error:
            logger.warning(
                "daily_delivery_not_claimed",
                delivery_id=delivery.id,
                error=str(error),
            )
            return self._report(delivery, DeliveryStep.NOT_DUE)
        if recovered:
            logger.warning(
                "daily_delivery_recovered",
                delivery_id=delivery.id,
                user_id=user_id,
                content_id=content.id,
                attempt_no=attempt.attempt_no,
                note="the previous attempt never reported back; the item may arrive twice",
            )
        return _Claim(
            delivery=delivery,
            attempt=attempt,
            content=content,
            lease_until=lease_until,
            recovered=recovered,
        )

    async def _may_receive(self, uow: UnitOfWork, user_id: int) -> bool:
        """Subscribed, not blocked, in the beta if there is one, and still entitled."""
        subscription = await uow.daily_subscriptions.get(user_id)
        if subscription is None or not subscription.receives_deliveries:
            return False
        beta = self.settings.beta_user_ids
        if beta and user_id not in beta:
            return False
        return has_daily_access(await uow.loyalty.count_completed(user_id))

    # ------------------------------------------------------------------- send

    async def _send(self, claim: _Claim) -> DeliveryReport:
        """Send outside any transaction, then record what Telegram said."""
        if self.clock() >= claim.lease_until:
            # The lease ran out while earlier items of the batch were being
            # sent: another worker may own the delivery now. Never send on an
            # expired lease — hand it back instead.
            return await self._hand_back(claim)
        return await self._send_now(claim)

    async def _send_now(self, claim: _Claim) -> DeliveryReport:
        """The Telegram call, and what its answer means for the delivery."""
        try:
            message_id = await self.sender.send(
                claim.delivery.user_id,
                claim.content,
                protect_content=self.settings.protect_content,
            )
        except DailySendTransientError as error:
            return await self._after_uncertain(claim, error)
        except DailySendBlockedError as error:
            return await self._after_blocked(claim, error)
        except DailyContentRejectedError as error:
            return await self._after_refused(claim, error, item_at_fault=True)
        except DailySendRejectedError as error:
            return await self._after_refused(claim, error, item_at_fault=False)
        except Exception as error:
            logger.error(  # noqa: TRY400 — recorded on the attempt, retried
                "daily_send_unexpected_failure",
                delivery_id=claim.delivery.id,
                error=repr(error),
            )
            return await self._after_uncertain(
                claim,
                DailySendTransientError(f"Unexpected failure: {error!r}"),
            )
        return await self._after_sent(claim, message_id)

    async def _hand_back(self, claim: _Claim) -> DeliveryReport:
        """Release an attempt that never reached Telegram; the same item stays due."""
        at = self.clock()
        async with self.uow_factory() as uow:
            done = await uow.daily_deliveries.finish_retry(
                claim.attempt.lease_token,
                at=at,
                outcome=DailyAttemptOutcome.TRANSIENT,
                error="The lease ran out before the send; nothing was sent",
                next_attempt_at=at,
            )
        if done is None:
            return self._lease_lost(claim)
        logger.warning(
            "daily_delivery_handed_back",
            delivery_id=claim.delivery.id,
            attempt_no=claim.attempt.attempt_no,
        )
        return self._report(claim, DeliveryStep.RETRY_SCHEDULED)

    async def _after_sent(self, claim: _Claim, message_id: int) -> DeliveryReport:
        async with self.uow_factory() as uow:
            done = await uow.daily_deliveries.finish_sent(
                claim.attempt.lease_token,
                at=self.clock(),
                telegram_message_id=message_id,
            )
        if done is None:
            return self._lease_lost(claim)
        logger.info(
            "daily_delivery_sent",
            delivery_id=claim.delivery.id,
            user_id=claim.delivery.user_id,
            content_id=claim.content.id,
            attempt_no=claim.attempt.attempt_no,
            recovered=claim.recovered,
        )
        return self._report(claim, DeliveryStep.SENT)

    async def _after_uncertain(
        self,
        claim: _Claim,
        error: DailySendTransientError,
    ) -> DeliveryReport:
        """Maybe delivered, maybe not: the same item again later, or give up the day."""
        at = self.clock()
        attempt_no = claim.attempt.attempt_no
        async with self.uow_factory() as uow:
            deliveries = uow.daily_deliveries
            if attempt_no >= self.settings.max_attempts:
                done = await deliveries.finish_failed(
                    claim.attempt.lease_token,
                    at=at,
                    outcome=DailyAttemptOutcome.TRANSIENT,
                    error=f"{error} — gave up after {attempt_no} attempts",
                )
                step = DeliveryStep.FAILED
            else:
                hint = timedelta(seconds=error.retry_after) if error.retry_after else None
                delay = retry_delay(
                    attempt_no,
                    initial=self.settings.retry_initial,
                    maximum=self.settings.retry_max,
                    jitter=self.jitter(),
                    retry_after=hint,
                )
                done = await deliveries.finish_retry(
                    claim.attempt.lease_token,
                    at=at,
                    outcome=DailyAttemptOutcome.TRANSIENT,
                    error=str(error),
                    next_attempt_at=at + delay,
                    retry_after_seconds=(
                        math.ceil(error.retry_after) if error.retry_after else None
                    ),
                )
                step = DeliveryStep.RETRY_SCHEDULED
        if done is None:
            return self._lease_lost(claim)
        logger.warning(
            "daily_delivery_uncertain",
            delivery_id=claim.delivery.id,
            attempt_no=attempt_no,
            step=step.value,
            error=str(error),
        )
        return self._report(claim, step, retry_after=error.retry_after)

    async def _after_blocked(self, claim: _Claim, error: DailySendBlockedError) -> DeliveryReport:
        """The buyer cannot receive: close the day, mark them, give the item back."""
        at = self.clock()
        async with self.uow_factory() as uow:
            done = await uow.daily_deliveries.finish_failed(
                claim.attempt.lease_token,
                at=at,
                outcome=DailyAttemptOutcome.BLOCKED,
                error=str(error),
            )
            # True whoever holds the delivery now: the buyer blocked the bot.
            await uow.daily_subscriptions.mark_blocked(claim.delivery.user_id, at=at)
        if done is None:
            return self._lease_lost(claim)
        logger.info(
            "daily_delivery_blocked",
            delivery_id=claim.delivery.id,
            user_id=claim.delivery.user_id,
        )
        return self._report(claim, DeliveryStep.BLOCKED)

    async def _after_refused(
        self,
        claim: _Claim,
        error: DailySendError,
        *,
        item_at_fault: bool,
    ) -> DeliveryReport:
        """Telegram definitely took nothing: give the item back, try another one."""
        at = self.clock()
        attempt_no = claim.attempt.attempt_no
        outcome = (
            DailyAttemptOutcome.CONTENT_INVALID if item_at_fault else DailyAttemptOutcome.PERMANENT
        )
        async with self.uow_factory() as uow:
            if item_at_fault and claim.content.status is DailyContentStatus.ACTIVE:
                # Nobody can receive it: out of deliveries until the owner looks.
                await uow.daily_content.set_status(
                    claim.content.id,
                    DailyContentStatus.DISABLED,
                    at=at,
                    reason=f"Telegram refused the item: {error}",
                )
            deliveries = uow.daily_deliveries
            if attempt_no >= self.settings.max_attempts:
                done = await deliveries.finish_failed(
                    claim.attempt.lease_token,
                    at=at,
                    outcome=outcome,
                    error=f"{error} — gave up after {attempt_no} attempts",
                )
                step = DeliveryStep.FAILED
            else:
                done = await deliveries.finish_rejected(
                    claim.attempt.lease_token,
                    at=at,
                    outcome=outcome,
                    error=str(error),
                    next_attempt_at=at,
                )
                step = DeliveryStep.ITEM_RELEASED
        if done is None:
            return self._lease_lost(claim)
        logger.warning(
            "daily_delivery_refused",
            delivery_id=claim.delivery.id,
            content_id=claim.content.id,
            item_at_fault=item_at_fault,
            step=step.value,
            error=str(error),
        )
        return self._report(claim, step)

    # ---------------------------------------------------------------- reports

    def _lease_lost(self, claim: _Claim) -> DeliveryReport:
        logger.warning(
            "daily_delivery_lease_lost",
            delivery_id=claim.delivery.id,
            attempt_no=claim.attempt.attempt_no,
        )
        return self._report(claim, DeliveryStep.LEASE_LOST)

    @staticmethod
    def _report(
        source: _Claim | DailyDelivery,
        step: DeliveryStep,
        *,
        retry_after: float | None = None,
    ) -> DeliveryReport:
        if isinstance(source, _Claim):
            return DeliveryReport(
                delivery_id=source.delivery.id,
                step=step,
                user_id=source.delivery.user_id,
                content_id=source.content.id,
                recovered=source.recovered,
                retry_after=retry_after,
            )
        return DeliveryReport(
            delivery_id=source.id,
            step=step,
            user_id=source.user_id,
            content_id=source.content_id,
        )


def _attemptable(delivery: DailyDelivery, now: datetime) -> bool:
    """Whether a pass may claim this delivery at ``now``."""
    if delivery.deadline_at <= now:
        return False
    if delivery.status in (DailyDeliveryStatus.PENDING, DailyDeliveryStatus.NO_CONTENT):
        return delivery.next_attempt_at <= now
    if delivery.status is DailyDeliveryStatus.SENDING:
        return delivery.lease_until is not None and delivery.lease_until <= now
    return False


__all__ = ["DailyDeliveryService", "DeliveryReport"]
