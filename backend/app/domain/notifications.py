"""Notification port for messages that are not a delivery.

``DeliveryGateway`` exists to hand over a purchased link and is deliberately
narrow. Telling an inviter that they earned bonuses is a different kind of
message: it is a courtesy, it is not owed to anybody, and it must never be able
to undo the thing it is announcing.

So it gets its own port, and its own rule: an implementation may fail, and the
caller treats that as a logged non-event. A reward that was written to the
ledger stays written even if the inviter has blocked the bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from decimal import Decimal

    from app.domain.enums import Currency


@dataclass(frozen=True, slots=True, kw_only=True)
class RewardNotice:
    """ "Your referral bought something and you earned bonuses."""

    user_id: int
    units: int
    """Bonus units credited by this one purchase."""

    balance: int
    """Balance after the credit, so the message can show it."""

    paid_amount: Decimal
    """What the referral actually paid — the base the reward was taken from."""

    currency: Currency


class RewardNotifier(Protocol):
    """Transport for the two courtesy messages the bonus feature sends."""

    async def notify_reward(self, notice: RewardNotice) -> None:
        """Tell an inviter they earned bonuses.

        May raise; the caller treats failure as a logged non-event.
        """
        ...

    async def notify_purchase_complete(self, user_id: int) -> None:
        """Offer the referral programme to somebody who just bought something.

        A separate message rather than an addition to the delivery itself: the
        delivery hands over a purchased link and is not touched by this feature.
        """
        ...


__all__ = ["RewardNotice", "RewardNotifier"]
