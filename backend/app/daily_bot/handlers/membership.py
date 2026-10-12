"""The bot's own membership in chats: blocked, unblocked, added to a group.

In a private chat Telegram reports the buyer blocking the bot (``kicked``) and
unblocking it (``member``); the subscription follows, quietly. The Daily bot
lives in private chats only, so being added to a group or channel makes it
leave at once.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Final

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError

from app.core.logging import get_logger
from app.daily_bot.middlewares import DailyServices

if TYPE_CHECKING:
    from aiogram.types import ChatMemberUpdated

logger = get_logger(__name__)

_PRESENT: Final = frozenset(
    {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
    }
)


async def handle_membership(event: ChatMemberUpdated, daily: DailyServices, bot: Bot) -> None:
    """Follow the buyer blocking or unblocking the bot; leave any group."""
    status = event.new_chat_member.status
    if event.chat.type != ChatType.PRIVATE:
        if status in _PRESENT:
            logger.info("daily_bot_leaves_group", chat_id=event.chat.id)
            with suppress(TelegramAPIError):
                await bot.leave_chat(chat_id=event.chat.id)
        return
    if status == ChatMemberStatus.KICKED:
        await daily.subscriptions.blocked(event.from_user.id)
    elif status == ChatMemberStatus.MEMBER:
        await daily.subscriptions.unblocked(event.from_user.id)


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="daily_membership")
    router.my_chat_member()(handle_membership)
    return router


__all__ = ["build_router"]
