"""``parse_owner_message``: what each kind of Telegram message becomes — no I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import (
    Animation,
    Audio,
    Chat,
    Contact,
    Dice,
    Document,
    Location,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginUser,
    Poll,
    PollOption,
    Sticker,
    Story,
    Venue,
    Voice,
)
from aiogram.types import VideoNote as TelegramVideoNote

from app.daily_bot import texts
from app.daily_bot.intake import Refusal, parse_owner_message
from app.domain.commands import DailyContentDraft
from app.domain.enums import DailyContentKind
from tests.daily_bot_harness import buyer, owner_message, photo_sizes, video

OWNER = 9800
CHANNEL = Chat(id=-1001234567890, type="channel", title="Source channel")


def _parse(**fields: Any) -> DailyContentDraft:
    parsed = parse_owner_message(owner_message(user=buyer(OWNER), message_id=77, **fields))
    assert isinstance(parsed, DailyContentDraft), parsed
    return parsed


def _refusal(**fields: Any) -> Refusal:
    parsed = parse_owner_message(owner_message(user=buyer(OWNER), **fields))
    assert isinstance(parsed, Refusal), parsed
    return parsed


# --------------------------------------------------------------------------- #
# The eight kinds                                                             #
# --------------------------------------------------------------------------- #


def test_a_text_keeps_its_formatting() -> None:
    draft = _parse(
        text="Важно: завтра новый выпуск",
        entities=[MessageEntity(type="bold", offset=0, length=5)],
    )

    assert draft.kind is DailyContentKind.TEXT
    assert draft.body == "Важно: завтра новый выпуск"
    assert draft.entities == ({"type": "bold", "offset": 0, "length": 5},)
    assert draft.file_id is None
    assert draft.file_unique_id is None


def test_a_photo_is_its_largest_size() -> None:
    draft = _parse(photo=photo_sizes("sunset"), caption="Закат")

    assert draft.kind is DailyContentKind.PHOTO
    assert draft.file_id == "sunset-1280"
    assert draft.file_unique_id == "sunset-unique-1280"
    assert (draft.width, draft.height, draft.file_size) == (1280, 853, 98_000)
    assert draft.body == "Закат"


def test_the_largest_photo_wins_in_whatever_order_the_sizes_come() -> None:
    draft = _parse(photo=list(reversed(photo_sizes("sunset"))))

    assert draft.file_id == "sunset-1280"


def test_a_video_keeps_its_metadata() -> None:
    draft = _parse(video=video("clip", duration=125), caption="Ролик")

    assert draft.kind is DailyContentKind.VIDEO
    assert draft.file_id == "clip-file"
    assert draft.file_unique_id == "clip-unique"
    assert (draft.duration, draft.width, draft.height) == (125, 1280, 720)
    assert (draft.file_name, draft.mime_type, draft.file_size) == (
        "clip.mp4",
        "video/mp4",
        4_200_000,
    )
    assert draft.body == "Ролик"


def test_a_gif_is_an_animation_although_telegram_also_calls_it_a_document() -> None:
    gif = Animation(
        file_id="gif-file",
        file_unique_id="gif-unique",
        width=480,
        height=270,
        duration=3,
        file_name="cat.gif.mp4",
        mime_type="video/mp4",
        file_size=310_000,
    )
    as_document = Document(
        file_id="gif-file",
        file_unique_id="gif-unique",
        file_name="cat.gif.mp4",
        mime_type="video/mp4",
    )

    draft = _parse(animation=gif, document=as_document)

    assert draft.kind is DailyContentKind.ANIMATION
    assert (draft.file_id, draft.duration, draft.width, draft.height) == ("gif-file", 3, 480, 270)


def test_a_document_keeps_its_name_and_type() -> None:
    draft = _parse(
        document=Document(
            file_id="doc-file",
            file_unique_id="doc-unique",
            file_name="Гайд.pdf",
            mime_type="application/pdf",
            file_size=1_048_576,
        ),
        caption="Гайд",
    )

    assert draft.kind is DailyContentKind.DOCUMENT
    assert (draft.file_name, draft.mime_type, draft.file_size) == (
        "Гайд.pdf",
        "application/pdf",
        1_048_576,
    )
    assert draft.body == "Гайд"


def test_an_audio_keeps_its_duration() -> None:
    draft = _parse(
        audio=Audio(
            file_id="song-file",
            file_unique_id="song-unique",
            duration=212,
            file_name="song.mp3",
            mime_type="audio/mpeg",
            file_size=5_000_000,
        ),
    )

    assert draft.kind is DailyContentKind.AUDIO
    assert (draft.duration, draft.file_name, draft.mime_type) == (212, "song.mp3", "audio/mpeg")


def test_a_voice_message_keeps_its_duration() -> None:
    draft = _parse(
        voice=Voice(
            file_id="voice-file",
            file_unique_id="voice-unique",
            duration=17,
            mime_type="audio/ogg",
            file_size=40_000,
        ),
    )

    assert draft.kind is DailyContentKind.VOICE
    assert (draft.file_id, draft.duration, draft.mime_type) == ("voice-file", 17, "audio/ogg")


def test_a_video_note_is_square_and_has_no_caption() -> None:
    draft = _parse(
        video_note=TelegramVideoNote(
            file_id="note-file",
            file_unique_id="note-unique",
            length=384,
            duration=9,
            file_size=700_000,
        ),
    )

    assert draft.kind is DailyContentKind.VIDEO_NOTE
    assert (draft.width, draft.height, draft.duration) == (384, 384, 9)
    assert draft.body is None
    assert draft.entities == ()


# --------------------------------------------------------------------------- #
# Captions and formatting                                                     #
# --------------------------------------------------------------------------- #


def test_a_caption_keeps_its_formatting() -> None:
    draft = _parse(
        photo=photo_sizes("p"),
        caption="Новинка недели",
        caption_entities=[
            MessageEntity(type="italic", offset=0, length=7),
            MessageEntity(type="text_link", offset=8, length=6, url="https://example.com"),
        ],
    )

    assert draft.entities == (
        {"type": "italic", "offset": 0, "length": 7},
        {"type": "text_link", "offset": 8, "length": 6, "url": "https://example.com"},
    )


@pytest.mark.parametrize("caption", [None, ""])
def test_no_caption_is_no_body(caption: str | None) -> None:
    draft = _parse(photo=photo_sizes("p"), caption=caption)

    assert draft.body is None
    assert draft.entities == ()


def test_custom_emoji_formatting_is_dropped_and_the_emoji_kept() -> None:
    body = "Смотри 🔥 тут"
    draft = _parse(
        photo=photo_sizes("p"),
        caption=body,
        caption_entities=[
            MessageEntity(type="bold", offset=0, length=6),
            MessageEntity(
                type="custom_emoji",
                offset=7,
                length=2,
                custom_emoji_id="5368324170671202286",
            ),
        ],
    )

    assert draft.body == body
    assert draft.entities == ({"type": "bold", "offset": 0, "length": 6},)


def test_custom_emoji_in_a_text_is_dropped_too() -> None:
    draft = _parse(
        text="🔥",
        entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1"),
        ],
    )

    assert draft.body == "🔥"
    assert draft.entities == ()


# --------------------------------------------------------------------------- #
# Where it came from                                                          #
# --------------------------------------------------------------------------- #


def test_the_source_is_the_owners_message_in_the_daily_chat() -> None:
    draft = _parse(video=video("clip"))

    assert (draft.source_chat_id, draft.source_message_id, draft.added_by) == (OWNER, 77, OWNER)


@pytest.mark.parametrize(
    "origin",
    [
        MessageOriginChannel(
            date=datetime(2026, 1, 1, tzinfo=UTC),
            chat=CHANNEL,
            message_id=555,
        ),
        MessageOriginUser(
            date=datetime(2026, 1, 1, tzinfo=UTC),
            sender_user=buyer(31337),
        ),
    ],
    ids=["from-a-channel", "from-a-person"],
)
def test_a_forward_is_parsed_like_the_original_and_sourced_in_the_daily_chat(
    origin: MessageOriginChannel | MessageOriginUser,
) -> None:
    original = _parse(video=video("clip"), caption="Ролик")
    forwarded = _parse(video=video("clip"), caption="Ролик", forward_origin=origin)

    assert forwarded == original
    assert (forwarded.source_chat_id, forwarded.source_message_id) == (OWNER, 77)


# --------------------------------------------------------------------------- #
# What is refused                                                             #
# --------------------------------------------------------------------------- #

_LOCATION = Location(latitude=55.75, longitude=37.62)
UNSUPPORTED: list[tuple[str, dict[str, Any]]] = [
    (
        "sticker",
        {
            "sticker": Sticker(
                file_id="sticker-file",
                file_unique_id="sticker-unique",
                type="regular",
                width=512,
                height=512,
                is_animated=False,
                is_video=False,
            )
        },
    ),
    (
        "poll",
        {
            "poll": Poll(
                id="poll-1",
                question="Да?",
                options=[
                    PollOption(persistent_id="yes", text="Да", voter_count=0),
                    PollOption(persistent_id="no", text="Нет", voter_count=0),
                ],
                total_voter_count=0,
                is_closed=False,
                is_anonymous=True,
                type="regular",
                allows_multiple_answers=False,
                allows_revoting=False,
                members_only=False,
            )
        },
    ),
    ("location", {"location": _LOCATION}),
    ("venue", {"venue": Venue(location=_LOCATION, title="Офис", address="Москва")}),
    ("contact", {"contact": Contact(phone_number="+70000000000", first_name="Иван")}),
    ("dice", {"dice": Dice(emoji="🎲", value=4)}),
    ("story", {"story": Story(chat=CHANNEL, id=7)}),
]


@pytest.mark.parametrize(("content_type", "fields"), UNSUPPORTED, ids=[c for c, _ in UNSUPPORTED])
def test_unsupported_messages_are_refused(content_type: str, fields: dict[str, Any]) -> None:
    refusal = _refusal(**fields)

    assert refusal == Refusal(content_type=content_type)
    assert not refusal.protected


def test_a_message_with_nothing_the_library_knows_is_refused() -> None:
    refusal = _refusal()

    assert not refusal.protected
    assert texts.refused(refusal) != texts.refused(Refusal(content_type="sticker"))


@pytest.mark.parametrize(
    "fields",
    [
        {"photo": photo_sizes("p")},
        {"video": video("clip")},
        {"text": "Секрет"},
    ],
    ids=["photo", "video", "text"],
)
def test_protected_content_is_refused(fields: dict[str, Any]) -> None:
    refusal = _refusal(has_protected_content=True, **fields)

    assert refusal.protected
