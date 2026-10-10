"""The owner's side of the Daily Content bot.

The owner's accounts (``DAILY_ADMIN_TELEGRAM_IDS``) never go through the
buyer's flow: they are not activated and receive no items. For now they get a
short note; adding content through the bot comes next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router

from app.daily_bot.texts import OWNER_HELP

if TYPE_CHECKING:
    from aiogram.types import Message


async def handle_owner_message(message: Message) -> None:
    """Any private message from the owner."""
    await message.answer(OWNER_HELP)


def build_router(owner_ids: tuple[int, ...]) -> Router:
    """A router for the owner's private messages; registered before the buyer's."""
    router = Router(name="daily_owner")
    router.message(F.chat.type == "private", F.from_user.id.in_(owner_ids))(handle_owner_message)
    return router


__all__ = ["build_router"]
