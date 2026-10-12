"""Telegram implementation of the Daily Content sender.

Items are sent by their ``file_id`` with the method of their kind — nothing is
downloaded, nothing is stored outside Telegram. Formatting travels as Telegram's
own entities, so ``parse_mode`` is switched off explicitly: a bot-wide default
would otherwise make Telegram re-parse the text.

If Telegram refuses an item's file, the owner's original message is copied
instead — once. Only when that is refused too is the item reported as unusable.
Every failure leaves here classified by ``to_daily_send_error``: definite
(blocked, rejected) or uncertain (transient).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram.methods import (
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVideoNote,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import Message, MessageEntity

from app.core.exceptions import (
    DailyContentRejectedError,
    DailySendError,
    DailySendRejectedError,
)
from app.core.logging import get_logger
from app.domain.enums import DailyContentKind
from app.infrastructure.telegram.errors import to_daily_send_error

if TYPE_CHECKING:
    from aiogram import Bot

    from app.domain.entities import DailyContent

logger = get_logger(__name__)

_MEDIA_METHODS: Final[dict[DailyContentKind, tuple[type[TelegramMethod[Message]], str]]] = {
    DailyContentKind.PHOTO: (SendPhoto, "photo"),
    DailyContentKind.VIDEO: (SendVideo, "video"),
    DailyContentKind.ANIMATION: (SendAnimation, "animation"),
    DailyContentKind.DOCUMENT: (SendDocument, "document"),
    DailyContentKind.AUDIO: (SendAudio, "audio"),
    DailyContentKind.VOICE: (SendVoice, "voice"),
    DailyContentKind.VIDEO_NOTE: (SendVideoNote, "video_note"),
}
"""The Telegram method for each media kind, and the field that carries the file."""

_CAPTIONLESS: Final = frozenset({DailyContentKind.VIDEO_NOTE})
"""Kinds Telegram sends without a caption."""


class TelegramDailyContentSender:
    """Sends Daily Content items through one bot."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        """Send the item; return the id of the message the buyer received."""
        failure: DailySendError
        try:
            request = self._request(chat_id, content, protect_content=protect_content)
        except ValueError as error:
            # Stored fields Telegram's own models refuse: the item, not the chat.
            failure = DailyContentRejectedError(f"The item cannot be sent as stored: {error}")
        else:
            try:
                message = await self._bot(request)
            except Exception as error:
                failure = to_daily_send_error(error)
            else:
                return message.message_id

        if not isinstance(failure, DailyContentRejectedError):
            raise failure
        return await self._copy_original(
            chat_id,
            content,
            protect_content=protect_content,
            refusal=failure,
        )

    async def _copy_original(
        self,
        chat_id: int,
        content: DailyContent,
        *,
        protect_content: bool,
        refusal: DailyContentRejectedError,
    ) -> int:
        """The file was refused: copy the owner's message it came from, once.

        Safe to try: the refusal was definite, so the buyer has nothing yet.
        """
        try:
            copied = await self._bot.copy_message(
                chat_id=chat_id,
                from_chat_id=content.source_chat_id,
                message_id=content.source_message_id,
                protect_content=protect_content,
            )
        except Exception as error:
            fallback = to_daily_send_error(error)
            if isinstance(fallback, DailyContentRejectedError | DailySendRejectedError):
                raise refusal from error
            raise fallback from error
        logger.warning(
            "daily_content_sent_by_copy",
            content_id=content.id,
            chat_id=chat_id,
            refusal=str(refusal),
        )
        return copied.message_id

    @staticmethod
    def _request(
        chat_id: int,
        content: DailyContent,
        *,
        protect_content: bool,
    ) -> TelegramMethod[Message]:
        """The one Telegram call that sends the item, for its kind."""
        entities = [MessageEntity.model_validate(entity) for entity in content.entities] or None
        if content.kind is DailyContentKind.TEXT:
            return SendMessage(
                chat_id=chat_id,
                text=content.body or "",
                entities=entities,
                parse_mode=None,
                protect_content=protect_content,
            )
        method, file_field = _MEDIA_METHODS[content.kind]
        payload: dict[str, object] = {
            "chat_id": chat_id,
            file_field: content.file_id,
            "protect_content": protect_content,
        }
        if content.kind not in _CAPTIONLESS:
            payload |= {"caption": content.body, "caption_entities": entities, "parse_mode": None}
        return method.model_validate(payload)


__all__ = ["TelegramDailyContentSender"]
