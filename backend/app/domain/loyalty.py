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
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from app.domain.enums import LoyaltyRewardKind, LoyaltyRewardStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.domain.entities import LoyaltyReward

DISCOUNT_MILESTONE: Final = 5
"""Completed purchases that earn one 50% discount on a later paid pack."""

FREE_PACK_MILESTONE: Final = 10
"""Completed purchases that earn one free pack."""

VIP_MILESTONE: Final = 15
"""Completed purchases from which a buyer is VIP. A status, not a reward."""

REWARD_MILESTONES: Final[dict[int, LoyaltyRewardKind]] = {
    DISCOUNT_MILESTONE: LoyaltyRewardKind.DISCOUNT_50_PERCENT,
    FREE_PACK_MILESTONE: LoyaltyRewardKind.FREE_PACK,
}
"""The milestones that grant a one-time reward, and what each grants."""


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


__all__ = [
    "DISCOUNT_MILESTONE",
    "FREE_PACK_MILESTONE",
    "REWARD_MILESTONES",
    "VIP_MILESTONE",
    "LoyaltyProgress",
    "MilestoneRewardState",
    "progress_of",
    "reward_state",
]
