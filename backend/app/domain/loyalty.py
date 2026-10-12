"""Loyalty programme: milestones and a buyer's progress towards them.

A *completed* purchase is one the buyer received: ``delivered``, or
``refunded`` after it had been delivered. Progress is that count, read from
the purchases every time — never a stored counter that could drift from them.

Two milestones grant a one-time reward (5 → half off one later paid pack,
10 → one free pack). VIP (15) is a status read off the same count, never a
stored reward.

A reward is recorded lazily, only when it is first actually used, so a buyer
who crossed a milestone long ago — before the programme existed, even — has no
row and still sees the reward as available. Reading progress therefore never
writes anything. Everything here is pure: the arithmetic of what the count and
the recorded rewards mean, with no database in sight.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from app.domain.bonuses import PriceQuote, floor_to_step
from app.domain.enums import Currency, LoyaltyRewardKind, LoyaltyRewardStatus

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from app.domain.entities import LoyaltyReward, Product, Purchase
    from app.domain.uow import UnitOfWork

DISCOUNT_MILESTONE: Final = 5
"""Completed purchases that earn one 50% discount on a later paid pack."""

FREE_PACK_MILESTONE: Final = 10
"""Completed purchases that earn one free pack."""

VIP_MILESTONE: Final = 15
"""Completed purchases from which a buyer is VIP. A status, not a reward.

VIP opens the Daily Content bot."""

CONTENT_SUGGESTION_MILESTONE: Final = 20
"""Completed purchases from which a buyer may suggest Daily Content."""

REWARD_MILESTONES: Final[dict[int, LoyaltyRewardKind]] = {
    DISCOUNT_MILESTONE: LoyaltyRewardKind.DISCOUNT_50_PERCENT,
    FREE_PACK_MILESTONE: LoyaltyRewardKind.FREE_PACK,
}
"""The milestones that grant a one-time reward, and what each grants."""

LOYALTY_DISCOUNT_PERCENT: Final = 50
"""Share of a list price the milestone-5 reward takes off one paid pack."""

ANNOUNCED_MILESTONES: Final = (DISCOUNT_MILESTONE, FREE_PACK_MILESTONE, VIP_MILESTONE)
"""Ranks at which a delivery tells the buyer what they have just reached."""

PROGRESS_LOCK_NAMESPACE: Final = "loyalty_progress"
"""Prefix of the per-buyer lock that serialises deliveries while ranking them."""

_PERCENT: Final = Decimal(100)


def loyalty_discount_for(base_amount: Decimal, currency: Currency) -> Decimal:
    """Half of the list price, rounded down to something the rail can charge.

    Whole Stars on Telegram Stars, cents on USDT — the same flooring as every
    other reduction, so the discount never promises a fraction the invoice
    cannot carry. A price too small to halve (1 Star, 0.01 USDT) yields 0.
    """
    raw = base_amount * Decimal(LOYALTY_DISCOUNT_PERCENT) / _PERCENT
    return floor_to_step(raw, currency)


def loyalty_quote(
    base_amount: Decimal,
    currency: Currency,
    *,
    reward_id: UUID | None = None,
) -> PriceQuote | None:
    """The price with the loyalty discount, or ``None`` when it would be zero.

    ``None`` means "do not use the reward here": a discount of nothing must not
    spend the buyer's one 50% reward.
    """
    discount = loyalty_discount_for(base_amount, currency)
    if discount <= 0:
        return None
    return PriceQuote(
        base_amount=base_amount,
        currency=currency,
        loyalty_amount=discount,
        loyalty_reward_id=reward_id,
    )


FREE_PACK_INVOICE_PREFIX: Final = "loyalty:"
"""Prefix of a free pack's ``external_id``; the rest is the reward's id."""


def free_pack_value(product: Product) -> tuple[Decimal, Currency]:
    """What a free pack is worth on paper: its Stars price, else its USDT price.

    Only for the books — the pack's ``base_amount`` and the ``loyalty_amount``
    that waives all of it. Nothing is ever billed in this currency.

    Raises:
        ValueError: the product has no price at all, which the database forbids.
    """
    if product.price_stars is not None:
        return Decimal(product.price_stars), Currency.XTR
    if product.price_usdt is not None:
        return product.price_usdt, Currency.USDT
    message = f"Product {product.id} has no price to value a free pack at"
    raise ValueError(message)


def free_pack_external_id(reward_id: UUID) -> str:
    """The free pack's ``external_id``: one reward, one pack.

    Unique per provider in the database, so the same reward can never be turned
    into a second internal purchase, whatever happens above it.
    """
    return f"{FREE_PACK_INVOICE_PREFIX}{reward_id}"


def milestone_of(rank: int) -> int | None:
    """The milestone a delivery of this rank reaches, if any.

    ``rank`` is the delivery's place among the buyer's completed purchases —
    the 5th, 10th and 15th reach something; every other one reaches nothing.
    """
    return rank if rank in ANNOUNCED_MILESTONES else None


class MilestoneRewardState(StrEnum):
    """Where one milestone reward stands, as the buyer should see it.

    ``LOCKED`` — the milestone is not reached yet.
    ``AVAILABLE`` — reached and unused, whether or not a row was recorded yet.
    ``IN_USE`` — priced into an invoice that is still waiting to be paid.
    ``USED`` — spent on a paid purchase, or a free pack already handed over.
    """

    LOCKED = "locked"
    AVAILABLE = "available"
    IN_USE = "in_use"
    USED = "used"


_STATE_OF_RECORDED: Final[dict[LoyaltyRewardStatus, MilestoneRewardState]] = {
    LoyaltyRewardStatus.AVAILABLE: MilestoneRewardState.AVAILABLE,
    LoyaltyRewardStatus.APPLIED: MilestoneRewardState.IN_USE,
    LoyaltyRewardStatus.CONSUMED: MilestoneRewardState.USED,
}


def reward_state(
    completed_count: int,
    milestone: int,
    recorded: LoyaltyRewardStatus | None,
) -> MilestoneRewardState:
    """The state of one milestone reward, from the count and its row, if any.

    A recorded row is the truth once it exists. Without one the reward is
    available exactly when the milestone has been reached — which is how a
    buyer who qualified before anything was ever recorded still gets it.
    """
    if recorded is not None:
        return _STATE_OF_RECORDED[recorded]
    if completed_count >= milestone:
        return MilestoneRewardState.AVAILABLE
    return MilestoneRewardState.LOCKED


@dataclass(frozen=True, slots=True, kw_only=True)
class LoyaltyProgress:
    """Everything the "🏆 My progress" screen shows, and nothing it does not."""

    completed_count: int
    discount_50: MilestoneRewardState
    free_pack: MilestoneRewardState

    @property
    def discount_50_available(self) -> bool:
        """Whether the 50% discount is earned and still unused."""
        return self.discount_50 is MilestoneRewardState.AVAILABLE

    @property
    def free_pack_available(self) -> bool:
        """Whether the free pack is earned and still unclaimed."""
        return self.free_pack is MilestoneRewardState.AVAILABLE

    @property
    def vip(self) -> bool:
        """VIP is the count itself, never a stored reward."""
        return self.completed_count >= VIP_MILESTONE

    @property
    def to_discount(self) -> int:
        """Completed purchases still missing for the 50% discount."""
        return max(DISCOUNT_MILESTONE - self.completed_count, 0)

    @property
    def to_free(self) -> int:
        """Completed purchases still missing for the free pack."""
        return max(FREE_PACK_MILESTONE - self.completed_count, 0)

    @property
    def to_vip(self) -> int:
        """Completed purchases still missing for VIP."""
        return max(VIP_MILESTONE - self.completed_count, 0)


def progress_of(completed_count: int, rewards: Iterable[LoyaltyReward]) -> LoyaltyProgress:
    """Combine the completed count with whatever rewards were recorded so far."""
    recorded = {reward.milestone: reward.status for reward in rewards}
    return LoyaltyProgress(
        completed_count=completed_count,
        discount_50=reward_state(
            completed_count,
            DISCOUNT_MILESTONE,
            recorded.get(DISCOUNT_MILESTONE),
        ),
        free_pack=reward_state(
            completed_count,
            FREE_PACK_MILESTONE,
            recorded.get(FREE_PACK_MILESTONE),
        ),
    )


class LoyaltyPricing(Protocol):
    """How pricing reaches the loyalty programme's 50% discount.

    The read-only methods answer for a card or a preview; the others run inside
    the caller's transaction, next to the purchase they concern, so the reward
    and the sale can never disagree.
    """

    @property
    def enabled(self) -> bool:
        """Whether the programme may price anything right now."""
        ...

    async def discount_available(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether the 50% reward is due and unused. Read only; records nothing."""
        ...

    async def lock_discount(self, uow: UnitOfWork, user_id: int) -> UUID | None:
        """For a checkout: record the reward if it is due, lock it, return its id.

        ``None`` when there is nothing to use (not reached, already in use or
        already used). The lock is held until the caller's transaction ends.
        """
        ...

    async def apply_discount(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        """Mark the locked reward as priced into ``purchase`` (same transaction)."""
        ...

    async def consume_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """The purchase was paid: its reward is spent for good. Idempotent."""
        ...

    async def release_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """The purchase expired unpaid: its reward is available again. Idempotent."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class FreePackGrant:
    """The internal purchase a free pack became, and whether it already existed."""

    purchase: Purchase
    repeated: bool = False
    """``True`` when this press found the pack it had already handed over."""


class FreePackRewards(Protocol):
    """How the purchase side reaches the milestone-10 free pack.

    Everything runs inside the caller's transaction, next to the internal
    purchase it concerns: the reward and the pack commit together or not at all.
    """

    @property
    def enabled(self) -> bool:
        """Whether the programme may hand anything out right now."""
        ...

    async def free_pack_available(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether the free pack is due and unclaimed. Read only; records nothing."""
        ...

    async def lock_free_pack(self, uow: UnitOfWork, user_id: int) -> LoyaltyReward | None:
        """Record the reward if it is due, lock it and return it, whatever its state.

        ``None`` when the milestone is not reached. The lock is held until the
        caller's transaction ends.
        """
        ...

    async def consume_free_pack(
        self,
        uow: UnitOfWork,
        reward: LoyaltyReward,
        purchase: Purchase,
    ) -> None:
        """Mark the locked reward as handed over as ``purchase`` (same transaction)."""
        ...


class LoyaltyMilestones(Protocol):
    """How a delivery learns which milestone it reached.

    Both run inside the delivery's own transaction: the lock first, then — only
    for a genuine ``paid → delivered`` transition — the rank.
    """

    @property
    def enabled(self) -> bool:
        """Whether the programme may rank anything right now."""
        ...

    async def lock_progress(self, uow: UnitOfWork, user_id: int) -> None:
        """Serialise this buyer's deliveries until the transaction ends."""
        ...

    async def milestone_reached(self, uow: UnitOfWork, user_id: int) -> int | None:
        """The milestone the delivery just recorded reaches, if any."""
        ...


class PurchaseLoyalty(FreePackRewards, LoyaltyMilestones, Protocol):
    """Everything the purchase side asks of the loyalty programme."""


class MilestoneNotifier(Protocol):
    """Port: tell a buyer which milestone a delivery just reached.

    A courtesy, never a transaction. Called only after the delivery committed,
    and whatever it raises is logged and forgotten.
    """

    async def notify_milestone(self, user_id: int, milestone: int) -> None:
        """Send the milestone notice."""
        ...


__all__ = [
    "ANNOUNCED_MILESTONES",
    "CONTENT_SUGGESTION_MILESTONE",
    "DISCOUNT_MILESTONE",
    "FREE_PACK_INVOICE_PREFIX",
    "FREE_PACK_MILESTONE",
    "LOYALTY_DISCOUNT_PERCENT",
    "PROGRESS_LOCK_NAMESPACE",
    "REWARD_MILESTONES",
    "VIP_MILESTONE",
    "FreePackGrant",
    "FreePackRewards",
    "LoyaltyMilestones",
    "LoyaltyPricing",
    "LoyaltyProgress",
    "MilestoneNotifier",
    "MilestoneRewardState",
    "PurchaseLoyalty",
    "free_pack_external_id",
    "free_pack_value",
    "loyalty_discount_for",
    "loyalty_quote",
    "milestone_of",
    "progress_of",
    "reward_state",
]
