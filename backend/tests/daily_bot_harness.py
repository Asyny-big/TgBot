"""Test doubles for the Daily Content bot: a recording bot and update builders.

Kept apart from ``bot_harness`` (the shop bot's) so neither grows the other's
concerns. Nothing here talks to Telegram.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot
from aiogram.methods import (
    CopyMessage,
    GetMe,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVideoNote,
    SendVoice,
)
from aiogram.types import (
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageId,
    Update,
    User,
)

if TYPE_CHECKING:
    from aiogram.methods import TelegramMethod

DAILY_BOT_TOKEN: Final = "987654321:AAHfake-Daily-Token_for_unit_tests_01"  # noqa: S105
DAILY_BOT_USER: Final = User(id=987654321, is_bot=True, first_name="Daily", username="VipDailyBot")

ITEM_METHODS: Final = (
    SendPhoto,
    SendVideo,
    SendAnimation,
    SendDocument,
    SendAudio,
    SendVoice,
    SendVideoNote,
    CopyMessage,
)
"""The calls that put a library item in front of a buyer."""

_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def next_update_id() -> int:
    return next(_update_ids)


class DailyRecordingBot(Bot):
    """A Bot whose API calls are recorded instead of sent.

    ``failures`` maps a method type to the errors its next calls raise, in order.
    """

    def __init__(self) -> None:
        super().__init__(token=DAILY_BOT_TOKEN)
        self.calls: list[TelegramMethod[Any]] = []
        self.failures: dict[type[TelegramMethod[Any]], list[Exception]] = {}

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        del request_timeout
        queued = self.failures.get(type(method))
        if queued:
            raise queued.pop(0)
        self.calls.append(method)
        if isinstance(method, GetMe):
            return DAILY_BOT_USER
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(_message_ids))
        if isinstance(method, (SendMessage, *ITEM_METHODS)):
            chat_id = int(getattr(method, "chat_id", 0))
            return Message(
                message_id=next(_message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
            )
        return True

    # -- assertion helpers --------------------------------------------------- #
    def texts_to(self, chat_id: int) -> list[str]:
        """Every text message sent to one chat, in order."""
        return [
            str(call.text)
            for call in self.calls
            if isinstance(call, SendMessage) and call.chat_id == chat_id
        ]

    def items_to(self, chat_id: int) -> list[TelegramMethod[Any]]:
        """Every library item sent to one chat, in order."""
        return [
            call
            for call in self.calls
            if isinstance(call, ITEM_METHODS) and getattr(call, "chat_id", None) == chat_id
        ]

    def sent_to(self, chat_id: int) -> list[TelegramMethod[Any]]:
        """Every text or item sent to one chat, in order."""
        return [
            call
            for call in self.calls
            if isinstance(call, (SendMessage, *ITEM_METHODS))
            and getattr(call, "chat_id", None) == chat_id
        ]


def buyer(telegram_id: int, username: str | None = "buyer") -> User:
    return User(id=telegram_id, is_bot=False, first_name="Buyer", username=username)


def message_update(
    text: str,
    *,
    user: User,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> Update:
    """A text message from ``user``; a private chat unless said otherwise."""
    chat = Chat(id=chat_id if chat_id is not None else user.id, type=chat_type, title=None)
    return Update(
        update_id=next_update_id(),
        message=Message(
            message_id=next(_message_ids),
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
        ),
    )


def membership_update(
    *,
    user: User,
    status: str,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> Update:
    """Telegram telling the bot its own membership changed (blocked, unblocked, added)."""
    new_member: ChatMemberBanned | ChatMemberMember | ChatMemberLeft
    if status == "kicked":
        new_member = ChatMemberBanned(user=DAILY_BOT_USER, until_date=datetime.now(UTC))
    elif status == "left":
        new_member = ChatMemberLeft(user=DAILY_BOT_USER)
    else:
        new_member = ChatMemberMember(user=DAILY_BOT_USER)
    old_member = ChatMemberLeft(user=DAILY_BOT_USER)
    chat = Chat(
        id=chat_id if chat_id is not None else user.id,
        type=chat_type,
        title=None if chat_type == "private" else "Some group",
    )
    return Update(
        update_id=next_update_id(),
        my_chat_member=ChatMemberUpdated(
            chat=chat,
            from_user=user,
            date=datetime.now(UTC),
            old_chat_member=old_member,
            new_chat_member=new_member,
        ),
    )


__all__ = [
    "DAILY_BOT_TOKEN",
    "DAILY_BOT_USER",
    "ITEM_METHODS",
    "DailyRecordingBot",
    "buyer",
    "membership_update",
    "message_update",
    "next_update_id",
]
