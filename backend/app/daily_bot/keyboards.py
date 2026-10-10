"""The Daily Content bot's keyboards: the buyer's menu, the owner's buttons."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.daily_bot import texts
from app.domain.daily import LibraryAction
from app.domain.enums import DailyContentStatus

if TYPE_CHECKING:
    from app.domain.entities import DailyContent


def menu_keyboard(*, paused: bool) -> ReplyKeyboardMarkup:
    """ "📊 My status", and "⏸ Pause" or "▶️ Resume" — whichever applies."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.STATUS_BUTTON)],
            [KeyboardButton(text=texts.RESUME_BUTTON if paused else texts.PAUSE_BUTTON)],
        ],
        resize_keyboard=True,
    )


# --------------------------------------------------------------------------- #
# The owner's library                                                         #
# --------------------------------------------------------------------------- #


class LibraryButton(StrEnum):
    """What an owner's button asks for."""

    DISABLE = "disable"
    ENABLE = "enable"
    ARCHIVE = "archive"
    UNDO = "undo"
    RESTORE = "restore"
    SHOW = "show"
    ADD_TEXT = "add_text"
    SKIP_TEXT = "skip_text"


LIBRARY_ACTIONS: Final = {
    LibraryButton.DISABLE: LibraryAction.DISABLE,
    LibraryButton.ENABLE: LibraryAction.ENABLE,
    LibraryButton.ARCHIVE: LibraryAction.ARCHIVE,
    LibraryButton.UNDO: LibraryAction.UNDO,
    LibraryButton.RESTORE: LibraryAction.RESTORE,
}
"""The buttons that change an item's status, and the action each one is."""


class LibraryCallback(CallbackData, prefix="dc"):
    """An owner's button: what to do, and to which item (``0`` for none yet)."""

    button: LibraryButton
    content_id: int = 0


def _button(text: str, button: LibraryButton, content_id: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=LibraryCallback(button=button, content_id=content_id).pack(),
    )


def added_keyboard(item: DailyContent) -> InlineKeyboardMarkup:
    """Under "✅ Added": take it back, or switch it off."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(texts.UNDO_BUTTON, LibraryButton.UNDO, item.id),
                _button(texts.DISABLE_BUTTON, LibraryButton.DISABLE, item.id),
            ]
        ]
    )


def card_keyboard(item: DailyContent) -> InlineKeyboardMarkup:
    """Under an item's card: what its status allows, and a preview."""
    if item.status is DailyContentStatus.ACTIVE:
        actions = [
            _button(texts.DISABLE_BUTTON, LibraryButton.DISABLE, item.id),
            _button(texts.ARCHIVE_BUTTON, LibraryButton.ARCHIVE, item.id),
        ]
    elif item.status is DailyContentStatus.DISABLED:
        actions = [
            _button(texts.ENABLE_BUTTON, LibraryButton.ENABLE, item.id),
            _button(texts.ARCHIVE_BUTTON, LibraryButton.ARCHIVE, item.id),
        ]
    else:
        actions = [_button(texts.RESTORE_BUTTON, LibraryButton.RESTORE, item.id)]
    return InlineKeyboardMarkup(
        inline_keyboard=[actions, [_button(texts.SHOW_BUTTON, LibraryButton.SHOW, item.id)]]
    )


def text_keyboard() -> InlineKeyboardMarkup:
    """Under "Add this text?": the text itself is the message this one replies to."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(texts.ADD_TEXT_BUTTON, LibraryButton.ADD_TEXT),
                _button(texts.SKIP_TEXT_BUTTON, LibraryButton.SKIP_TEXT),
            ]
        ]
    )


__all__ = [
    "LIBRARY_ACTIONS",
    "LibraryButton",
    "LibraryCallback",
    "added_keyboard",
    "card_keyboard",
    "menu_keyboard",
    "text_keyboard",
]
