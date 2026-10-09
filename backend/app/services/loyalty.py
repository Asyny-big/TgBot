"""Loyalty programme use cases: progress, the 50% discount and the free pack.

``get_progress`` answers "how far is this buyer?" from their purchases and the
rewards recorded so far. It never writes: opening the progress screen leaves
the database exactly as it was, and a buyer who qualified before the programme
existed sees their rewards without anything being backfilled.

The milestone-5 reward — half off one later paid pack — moves through

    (due, no row) ≡ available ─apply─▶ applied(P) ─pay P─▶ consumed(P)
                                 applied(P) ─P expires─▶ available

and every move happens in the transaction that moves the purchase itself:
``apply`` with the purchase insert, ``consume`` with the payment, ``release``
with the expiry. The row is recorded lazily, the first time it is locked for a
checkout, and locked ``FOR UPDATE`` whenever it changes.

Settlement never fails because of the reward. Money that arrived is honoured
and the buyer gets their pack even if the reward is not where it should be —
the anomaly is logged, the same rule that settled referral Bug #2.

The milestone-10 reward — one pack for free — has no "applied" state:

    (due, no row) ≡ available ─claim─▶ consumed(P)

where P is the ``internal`` purchase created in the same transaction. Here
nothing is being paid for, so the opposite rule holds: if the reward cannot be
consumed, the pack is not handed over either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import AppError, ConflictError, LoyaltyDisabledError
from app.core.logging import get_logger
from app.domain.enums import LoyaltyRewardStatus
from app.domain.loyalty import (
    DISCOUNT_MILESTONE,
    FREE_PACK_MILESTONE,
    MilestoneRewardState,
    progress_of,
    reward_state,
)

if TYPE_CHECKING:
    from uuid import UUID

    from app.core.config import LoyaltySettings
    from app.domain.bonuses import PriceQuote
    from app.domain.entities import LoyaltyReward, Purchase
    from app.domain.loyalty import LoyaltyProgress
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LoyaltyService:
    """A buyer's loyalty progress, the 50% discount and the free pack."""

    uow_factory: UnitOfWorkFactory
    settings: LoyaltySettings

    @property
    def enabled(self) -> bool:
        """Whether the programme is switched on (``LOYALTY_ENABLED``)."""
        return self.settings.enabled

    # ---------------------------------------------------------------- progress

    async def get_progress(self, user_id: int) -> LoyaltyProgress:
        """Completed purchases, the two milestone rewards and VIP. Read only.

        Raises:
            LoyaltyDisabledError: the programme is switched off. Checked here as
                well as in the bot, so no caller can read it while it is off.
        """
        if not self.enabled:
            raise LoyaltyDisabledError(user_id=user_id)
        async with self.uow_factory() as uow:
            completed = await uow.loyalty.count_completed(user_id)
            rewards = await uow.loyalty.rewards_of(user_id)
        progress = progress_of(completed, rewards)
        logger.info(
            "loyalty_progress_read",
            user_id=user_id,
            completed=progress.completed_count,
            discount_50=progress.discount_50.value,
            free_pack=progress.free_pack.value,
            vip=progress.vip,
        )
        return progress

    # ------------------------------------------------- the 50% discount: price

    async def discount_available(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether the 50% reward is due and unused. Read only; records nothing."""
        return await self._available(uow, user_id, DISCOUNT_MILESTONE)

    async def lock_discount(self, uow: UnitOfWork, user_id: int) -> UUID | None:
        """Record the reward if it is due, lock it, and return it while available.

        The lock is what keeps two checkouts of different products — which hold
        different purchase locks — from both getting the one discount: the
        second waits here, then finds the reward applied.
        """
        if await uow.loyalty.count_completed(user_id) < DISCOUNT_MILESTONE:
            return None
        await uow.loyalty.ensure_reward(user_id, DISCOUNT_MILESTONE)
        reward = await uow.loyalty.lock_reward(user_id, DISCOUNT_MILESTONE)
        if reward is None or reward.status is not LoyaltyRewardStatus.AVAILABLE:
            return None
        return reward.id

    # ------------------------------------------------- the 50% discount: holds

    async def apply_discount(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        """``available → applied``, in the transaction that creates the purchase.

        Raises:
            ConflictError: the quote carries no locked reward, or the reward is
                not available. Either aborts the purchase insert with it.
        """
        if priced.loyalty_reward_id is None:
            message = "A loyalty discount reached checkout without a locked reward"
            raise ConflictError(message, purchase_id=str(purchase.id))
        await uow.loyalty.mark_applied(priced.loyalty_reward_id, purchase.id)
        logger.info(
            "loyalty_discount_applied",
            purchase_id=str(purchase.id),
            user_id=purchase.user_id,
            reward_id=str(priced.loyalty_reward_id),
            loyalty_amount=str(purchase.loyalty_amount),
        )

    async def consume_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """The purchase was paid: its reward is spent for good. Never raises.

        Idempotent: a replayed notification finds the reward already consumed by
        this purchase and changes nothing.
        """
        try:
            await self._consume(uow, purchase)
        except AppError as error:
            logger.error(  # noqa: TRY400 — money arrived; the reward must not stop delivery
                "loyalty_reward_not_consumed",
                purchase_id=str(purchase.id),
                user_id=purchase.user_id,
                error=str(error),
            )

    async def _consume(self, uow: UnitOfWork, purchase: Purchase) -> None:
        reward = await uow.loyalty.lock_reward(purchase.user_id, DISCOUNT_MILESTONE)
        if reward is None:
            logger.warning("loyalty_reward_missing_on_payment", purchase_id=str(purchase.id))
            return

        if reward.status is LoyaltyRewardStatus.CONSUMED:
            if reward.consumed_purchase_id != purchase.id:
                # A second paid purchase carries the one discount — only
                # possible through a race the checkout guards close. The money
                # moved; the buyer keeps their pack. Logged for follow-up.
                logger.warning(
                    "loyalty_reward_already_consumed",
                    purchase_id=str(purchase.id),
                    reward_id=str(reward.id),
                    consumed_purchase_id=str(reward.consumed_purchase_id),
                )
            return

        applied_elsewhere = (
            reward.status is LoyaltyRewardStatus.APPLIED
            and reward.applied_purchase_id != purchase.id
        )
        if applied_elsewhere:
            logger.warning(
                "loyalty_reward_reused",
                purchase_id=str(purchase.id),
                reward_id=str(reward.id),
                applied_purchase_id=str(reward.applied_purchase_id),
            )
        elif reward.status is LoyaltyRewardStatus.AVAILABLE:
            # Released when this invoice expired, then paid anyway: honour it.
            logger.warning(
                "loyalty_reward_consumed_after_release",
                purchase_id=str(purchase.id),
                reward_id=str(reward.id),
            )

        await uow.loyalty.mark_consumed(reward.id, purchase.id)
        logger.info(
            "loyalty_discount_consumed",
            purchase_id=str(purchase.id),
            user_id=purchase.user_id,
            reward_id=str(reward.id),
        )

    async def release_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """The purchase expired unpaid: its reward is available again. Idempotent."""
        reward = await uow.loyalty.lock_reward(purchase.user_id, DISCOUNT_MILESTONE)
        if reward is None:
            return
        released = await uow.loyalty.mark_released(reward.id, purchase.id)
        if released is not None:
            logger.info(
                "loyalty_discount_released",
                purchase_id=str(purchase.id),
                user_id=purchase.user_id,
                reward_id=str(reward.id),
            )

    async def release_stale_discounts(self, *, limit: int = 100) -> int:
        """Housekeeping: give back rewards whose invoice has since expired.

        Runs whether or not the programme is on: a reward already applied to an
        invoice must come back however the switch is set now.
        """
        async with self.uow_factory() as uow:
            released = await uow.loyalty.release_expired(limit=limit)
        if released:
            logger.info("loyalty_discounts_released", count=len(released))
        return len(released)

    # ------------------------------------------------------------ free pack

    async def free_pack_available(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether the free pack is due and unclaimed. Read only; records nothing."""
        return await self._available(uow, user_id, FREE_PACK_MILESTONE)

    async def lock_free_pack(self, uow: UnitOfWork, user_id: int) -> LoyaltyReward | None:
        """Record the free pack if it is due, lock it, and return it in any state.

        The caller needs the consumed state too: a second press for the pack
        already handed over is answered with that pack, not refused. The lock
        is what keeps two claims for different products — which hold different
        purchase locks — from both getting the one pack.
        """
        if await uow.loyalty.count_completed(user_id) < FREE_PACK_MILESTONE:
            return None
        await uow.loyalty.ensure_reward(user_id, FREE_PACK_MILESTONE)
        return await uow.loyalty.lock_reward(user_id, FREE_PACK_MILESTONE)

    async def consume_free_pack(
        self,
        uow: UnitOfWork,
        reward: LoyaltyReward,
        purchase: Purchase,
    ) -> None:
        """``available → consumed``, in the transaction that creates the pack.

        Raises:
            ConflictError: the reward is no longer available. It aborts the
                internal purchase with it: no reward, no free pack.
        """
        await uow.loyalty.mark_consumed(reward.id, purchase.id)
        logger.info(
            "loyalty_free_pack_consumed",
            purchase_id=str(purchase.id),
            user_id=purchase.user_id,
            reward_id=str(reward.id),
        )

    # ----------------------------------------------------------------- shared

    async def _available(self, uow: UnitOfWork, user_id: int, milestone: int) -> bool:
        """Whether one milestone reward is due and unused, from the count and its row."""
        completed = await uow.loyalty.count_completed(user_id)
        if completed < milestone:
            return False
        rewards = await uow.loyalty.rewards_of(user_id)
        recorded = {reward.milestone: reward.status for reward in rewards}
        state = reward_state(completed, milestone, recorded.get(milestone))
        return state is MilestoneRewardState.AVAILABLE


__all__ = ["LoyaltyService"]
