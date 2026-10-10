"""The Daily Content bot's menu: a reply keyboard, always the buyer's own."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.daily_bot.texts import PAUSE_BUTTON, RESUME_BUTTON, STATUS_BUTTON


def menu_keyboard(*, paused: bool) -> ReplyKeyboardMarkup:
    """ "📊 My status", and "⏸ Pause" or "▶️ Resume" — whichever applies."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=STATUS_BUTTON)],
            [KeyboardButton(text=RESUME_BUTTON if paused else PAUSE_BUTTON)],
        ],
        resize_keyboard=True,
    )


__all__ = ["menu_keyboard"]
