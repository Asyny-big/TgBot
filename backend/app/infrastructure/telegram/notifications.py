"""Telegram implementation of the bonus notification port.

Both messages this sends are courtesies. Neither is allowed to matter: a reward
that reached the ledger stays there even if the inviter has blocked the bot, and
a buyer who never sees the referral invitation has still received everything
they paid for.

So this class does not classify failures the way ``TelegramDeliveryGateway``
does. There is no retry, no transient-versus-permanent distinction and no
back-off: the caller logs whatever comes out and moves on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.bot.keyboards import bonus_hint_keyboard
from app.bot.texts import PURCHASE_COMPLETE_HINT, reward_notice
from app.core.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

    from app.domain.notifications import RewardNotice

logger = get_logger(__name__)


class TelegramBonusNotifier:
    """Sends the reward notice and the post-purchase referral invitation."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def notify_reward(self, notice: RewardNotice) -> None:
        """Tell an inviter that a referral's purchase earned them bonuses.

        Sent once per purchase, because the caller only reaches this after the
        ledger entry was genuinely created — a replayed payment notification
        writes no entry and therefore sends no message.
        """
        await self._bot.send_message(
            chat_id=notice.user_id,
            text=reward_notice(
                units=notice.units,
                balance=notice.balance,
                paid_amount=notice.paid_amount,
            ),
            disable_web_page_preview=True,
        )
        logger.info("reward_notice_sent", user_id=notice.user_id, units=notice.units)

    async def notify_purchase_complete(self, user_id: int) -> None:
        """Offer the referral programme after a completed purchase."""
        await self._bot.send_message(
            chat_id=user_id,
            text=PURCHASE_COMPLETE_HINT,
            reply_markup=bonus_hint_keyboard(),
            disable_web_page_preview=True,
        )
        logger.info("purchase_hint_sent", user_id=user_id)


__all__ = ["TelegramBonusNotifier"]
