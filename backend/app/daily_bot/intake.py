"""What an owner's message becomes in the Daily Content library — pure, no I/O.

``parse_owner_message`` reads one Telegram message and returns either the
library item it describes or the reason it cannot be one. Nothing is
downloaded: an item is Telegram's own ``file_id`` plus the facts worth
showing, and the message itself — the owner's, in the chat with the Daily bot,
forwarded or not — is kept as the source the delivery engine can copy from
when Telegram refuses the file.

Eight kinds are taken: text, photo, video, animation (GIF), document, audio,
voice and video note. Everything else — stickers, polls, locations, contacts,
stories, paid media, anything Telegram adds later — is refused, and so is a
message whose content is protected from copying.

``custom_emoji`` formatting is dropped: a bot may be refused it when sending.
The emoji itself stays in the text, as the plain character Telegram shows
in its place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from aiogram.enums import MessageEntityType

from app.domain.commands import DailyContentDraft
from app.domain.enums import DailyContentKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram.types import Message, MessageEntity

type Entities = tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class Refusal:
    """A message the library cannot take, and why."""

    content_type: str
    """Telegram's name for what was sent: ``sticker``, ``poll``, ..."""

    protected: bool = False
    """The message is protected from copying, so it cannot be sent on."""


type ContentDraftResult = DailyContentDraft | Refusal


@dataclass(frozen=True, slots=True)
class _Source:
    """What every item of one message shares: where it came from, its caption."""

    chat_id: int
    message_id: int
    added_by: int
    caption: str | None
    caption_entities: Entities


def parse_owner_message(message: Message) -> ContentDraftResult:
    """The library item ``message`` describes, or why it cannot be one."""
    if message.has_protected_content:
        return Refusal(content_type=_content_type(message), protected=True)
    caption, caption_entities = _formatted(message.caption, message.caption_entities)
    source = _Source(
        chat_id=message.chat.id,
        message_id=message.message_id,
        added_by=message.from_user.id if message.from_user is not None else message.chat.id,
        caption=caption,
        caption_entities=caption_entities,
    )
    for read in _READERS:
        draft = read(message, source)
        if draft is not None:
            return draft
    return Refusal(content_type=_content_type(message))


# --------------------------------------------------------------------------- #
# One reader per kind                                                         #
# --------------------------------------------------------------------------- #


def _animation(message: Message, source: _Source) -> DailyContentDraft | None:
    animation = message.animation
    if animation is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.ANIMATION,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=animation.file_id,
        file_unique_id=animation.file_unique_id,
        file_size=animation.file_size,
        file_name=animation.file_name,
        mime_type=animation.mime_type,
        duration=animation.duration,
        width=animation.width,
        height=animation.height,
    )


def _photo(message: Message, source: _Source) -> DailyContentDraft | None:
    if not message.photo:
        return None
    largest = max(message.photo, key=lambda size: (size.width * size.height, size.file_size or 0))
    return DailyContentDraft(
        kind=DailyContentKind.PHOTO,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=largest.file_id,
        file_unique_id=largest.file_unique_id,
        file_size=largest.file_size,
        width=largest.width,
        height=largest.height,
    )


def _video(message: Message, source: _Source) -> DailyContentDraft | None:
    video = message.video
    if video is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.VIDEO,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=video.file_id,
        file_unique_id=video.file_unique_id,
        file_size=video.file_size,
        file_name=video.file_name,
        mime_type=video.mime_type,
        duration=video.duration,
        width=video.width,
        height=video.height,
    )


def _video_note(message: Message, source: _Source) -> DailyContentDraft | None:
    note = message.video_note
    if note is None:
        return None
    # Round, and never captioned: its side is both its width and its height.
    return DailyContentDraft(
        kind=DailyContentKind.VIDEO_NOTE,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        file_id=note.file_id,
        file_unique_id=note.file_unique_id,
        file_size=note.file_size,
        duration=note.duration,
        width=note.length,
        height=note.length,
    )


def _voice(message: Message, source: _Source) -> DailyContentDraft | None:
    voice = message.voice
    if voice is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.VOICE,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=voice.file_id,
        file_unique_id=voice.file_unique_id,
        file_size=voice.file_size,
        mime_type=voice.mime_type,
        duration=voice.duration,
    )


def _audio(message: Message, source: _Source) -> DailyContentDraft | None:
    audio = message.audio
    if audio is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.AUDIO,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=audio.file_id,
        file_unique_id=audio.file_unique_id,
        file_size=audio.file_size,
        file_name=audio.file_name,
        mime_type=audio.mime_type,
        duration=audio.duration,
    )


def _document(message: Message, source: _Source) -> DailyContentDraft | None:
    document = message.document
    if document is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.DOCUMENT,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=source.caption,
        entities=source.caption_entities,
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_size=document.file_size,
        file_name=document.file_name,
        mime_type=document.mime_type,
    )


def _text(message: Message, source: _Source) -> DailyContentDraft | None:
    text, entities = _formatted(message.text, message.entities)
    if text is None:
        return None
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=source.chat_id,
        source_message_id=source.message_id,
        added_by=source.added_by,
        body=text,
        entities=entities,
    )


_READERS: Final[tuple[Callable[[Message, _Source], DailyContentDraft | None], ...]] = (
    _animation,  # before the document: Telegram sets ``document`` on a GIF too
    _photo,
    _video,
    _video_note,
    _voice,
    _audio,
    _document,
    _text,
)
"""Tried in order; the first that recognises the message describes it."""


def _content_type(message: Message) -> str:
    """``"sticker"``, ``"poll"``...: aiogram's enum member, as Telegram names it."""
    kind = message.content_type
    return kind.value if isinstance(kind, Enum) else str(kind)


def _formatted(
    text: str | None,
    entities: list[MessageEntity] | None,
) -> tuple[str | None, Entities]:
    """The text, or ``None`` when empty, and its formatting minus custom emoji."""
    if not text:
        return None, ()
    kept = tuple(
        entity.model_dump(mode="json", exclude_none=True)
        for entity in entities or ()
        if entity.type != MessageEntityType.CUSTOM_EMOJI
    )
    return text, kept


__all__ = ["ContentDraftResult", "Refusal", "parse_owner_message"]
