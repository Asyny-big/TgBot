"""Loyalty programme facts that the schema itself depends on.

Only the milestones live here for now: which number of completed purchases
earns which reward. The programme's behaviour — progress, rewards, pricing —
is not implemented yet and is switched off by ``LOYALTY_ENABLED``.

A *completed* purchase is one the buyer received: ``delivered``, or
``refunded`` after it had been delivered. VIP is a status read off that count,
never a stored reward.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import LoyaltyRewardKind

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

__all__ = [
    "DISCOUNT_MILESTONE",
    "FREE_PACK_MILESTONE",
    "REWARD_MILESTONES",
    "VIP_MILESTONE",
]
