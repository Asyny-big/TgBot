"""The "🏆 My progress" section of the loyalty programme.

Read only: it shows how many packs the buyer has received, which milestone
rewards are available, and how far VIP is. It offers no way to use a reward —
that is a separate step — and it writes nothing.

A VIP buyer the Daily bot would serve also finds "👑 Открыть VIP Daily"
under the screen (see ``DailyEntry``).

Everything here is behind ``LOYALTY_ENABLED``. With the programme off:

* ``/progress`` is not handled at all, so the bot answers it exactly as before
  the section existed (it does not);
* a progress button pressed anyway — an old message, or a hand-crafted
  callback — gets a neutral "not available" and reveals nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command

from app.bot.keyboards import ProgressCallback, daily_entry_keyboard
from app.bot.middlewares import BotServices
from app.bot.texts import PROGRESS_UNAVAILABLE, SECTION_UNAVAILABLE, loyalty_progress
from app.core.exceptions import AppError, LoyaltyDisabledError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message, TelegramObject

logger = get_logger(__name__)

PROGRESS_COMMAND = "progress"
"""Reachable without a product link, like ``/bonus`` — while the programme is on."""


async def loyalty_enabled(event: TelegramObject, shop: BotServices) -> bool:
    """Router filter: the section exists only while the programme is on."""
    del event
    return shop.loyalty.enabled


async def handle_progress_command(message: Message, shop: BotServices) -> None:
    """``/progress``: open the section."""
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return
    text, daily_link = await _render(shop, user.id)
    await message.answer(text, reply_markup=daily_entry_keyboard(daily_link))


async def handle_progress_pressed(callback: CallbackQuery, shop: BotServices) -> None:
    """The "🏆 My progress" button.

    The progress shown is always the sender's own: the callback carries no user
    id, so nobody can ask for anybody else's.
    """
    if not shop.loyalty.enabled:
        await callback.answer(SECTION_UNAVAILABLE, show_alert=False)
        return
    await callback.answer()
    if callback.bot is None:  # pragma: no cover — aiogram always binds the bot
        return
    text, daily_link = await _render(shop, callback.from_user.id)
    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=daily_entry_keyboard(daily_link),
    )


async def _render(shop: BotServices, user_id: int) -> tuple[str, str | None]:
    """The screen and the way into VIP Daily, or a soft failure that leaves the
    rest of the bot untouched."""
    try:
        progress = await shop.loyalty.get_progress(user_id)
    except LoyaltyDisabledError:  # pragma: no cover — both entry points check first
        return SECTION_UNAVAILABLE, None
    except AppError as error:
        logger.error(  # noqa: TRY400 — a soft failure, not a crash
            "progress_section_unavailable",
            telegram_id=user_id,
            error=str(error),
        )
        return PROGRESS_UNAVAILABLE, None
    daily_link = shop.daily.link_for(user_id, completed_purchases=progress.completed_count)
    return loyalty_progress(progress), daily_link


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="progress")
    private = F.chat.type == "private"
    router.message(Command(PROGRESS_COMMAND), private, loyalty_enabled)(handle_progress_command)
    router.callback_query(ProgressCallback.filter())(handle_progress_pressed)
    return router


__all__ = [
    "PROGRESS_COMMAND",
    "build_router",
    "handle_progress_command",
    "handle_progress_pressed",
    "loyalty_enabled",
]
