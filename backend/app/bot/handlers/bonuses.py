"""The "My bonuses" section and the invitation entry point.

This is the only part of the bot a user can reach without a product link, and it
is deliberately not a catalogue: it shows a balance, two counters and one
invitation link. There is no product list, no search, no recommendations and no
list of preview channels — the shop is still reached only through a deep link to
one specific product.

An invitation link is handled here too, and it never opens a product. It records
"this visitor was invited by that user" and points at the preview directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command

from app.bot.keyboards import (
    BonusCallback,
    bonus_section_keyboard,
    preview_keyboard,
    share_url,
)
from app.bot.middlewares import BotServices
from app.bot.profile import profile_of
from app.bot.texts import (
    BONUS_DISABLED,
    BONUS_UNAVAILABLE,
    REFERRAL_ALREADY_LINKED,
    REFERRAL_NOT_ELIGIBLE,
    REFERRAL_SELF,
    REFERRAL_UNKNOWN,
    bonus_section,
    referral_share_text,
    referral_welcome,
)
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.domain.referrals import ReferralOutcome

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

    from app.domain.referrals import ReferralRegistration

logger = get_logger(__name__)

BONUS_COMMAND = "bonus"
"""Reachable at any time, without a product link and without a purchase."""

_REFUSAL_TEXTS = {
    ReferralOutcome.ALREADY_LINKED: REFERRAL_ALREADY_LINKED,
    ReferralOutcome.SELF_REFERRAL: REFERRAL_SELF,
    ReferralOutcome.NOT_ELIGIBLE: REFERRAL_NOT_ELIGIBLE,
    ReferralOutcome.UNKNOWN_CODE: REFERRAL_UNKNOWN,
    ReferralOutcome.DISABLED: REFERRAL_UNKNOWN,
}


async def show_invitation(
    message: Message,
    shop: BotServices,
    registration: ReferralRegistration,
) -> None:
    """Answer an invitation link.

    A refused invitation is a normal situation and gets a plain sentence. An
    accepted one explains the discount and offers the preview directory, which
    is the only channel link the bot knows.
    """
    if not registration.is_linked:
        await message.answer(_REFUSAL_TEXTS[registration.outcome])
        return

    settings = shop.referrals.settings
    await message.answer(
        referral_welcome(
            discount_percent=settings.discount_percent,
            preview_available=bool(settings.preview_directory_url),
        ),
        reply_markup=preview_keyboard(settings.preview_directory_url),
    )


async def handle_bonus_command(message: Message, shop: BotServices) -> None:
    """``/bonus``: open the bonus section."""
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return
    text, markup = await _render_section(shop, user)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def handle_bonus_pressed(callback: CallbackQuery, shop: BotServices) -> None:
    """The "My bonuses" button, from wherever it was shown."""
    await callback.answer()
    if callback.bot is None:  # pragma: no cover — aiogram always binds the bot
        return
    text, markup = await _render_section(shop, callback.from_user)
    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def _render_section(
    shop: BotServices,
    user: User,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the section, or a soft failure that leaves the shop untouched."""
    referrals = shop.referrals
    if not referrals.enabled:
        return BONUS_DISABLED, None

    try:
        summary = await referrals.summary(profile_of(user))
    except AppError as error:
        # The bonus screen is an extra: it must never look like a broken shop.
        logger.error(  # noqa: TRY400 — a soft failure, not a crash
            "bonus_section_unavailable",
            telegram_id=user.id,
            error=str(error),
        )
        return BONUS_UNAVAILABLE, None

    link = referrals.link_for(summary.referral_code)
    text = bonus_section(
        balance=summary.balance,
        invited_count=summary.invited_count,
        referral_purchase_count=summary.referral_purchase_count,
        referral_link=link,
        reward_percent=referrals.settings.reward_percent,
    )
    markup = bonus_section_keyboard(
        share_url(
            referral_link=link,
            text=referral_share_text(
                referral_link=link,
                discount_percent=referrals.settings.discount_percent,
            ),
        )
    )
    return text, markup


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="bonuses")
    private = F.chat.type == "private"
    router.message(Command(BONUS_COMMAND), private)(handle_bonus_command)
    router.callback_query(BonusCallback.filter())(handle_bonus_pressed)
    return router


__all__ = [
    "BONUS_COMMAND",
    "build_router",
    "handle_bonus_command",
    "handle_bonus_pressed",
    "show_invitation",
]
