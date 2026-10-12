"""The Telegram side of Daily Content sending, without a network.

Which Telegram method each kind of item goes out with, how every failure is
classified — definitely not delivered, or uncertain — and the one fallback to
the owner's original message when Telegram refuses an item's file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import (
    CopyMessage,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVideoNote,
    SendVoice,
)
from aiogram.types import Chat, Message, MessageEntity, MessageId

from app.core.exceptions import (
    DailyContentRejectedError,
    DailySendBlockedError,
    DailySendRejectedError,
    DailySendTransientError,
)
from app.domain.entities import DailyContent
from app.domain.enums import DailyContentKind, DailyContentStatus
from app.infrastructure.telegram.daily import TelegramDailyContentSender
from app.infrastructure.telegram.errors import to_daily_send_error

if TYPE_CHECKING:
    from aiogram.methods import TelegramMethod

BOT_TOKEN = "123456789:AAHfake-Test-Token_for_unit_tests_only01"  # noqa: S105
BUYER = 4242
OWNER_CHAT = 777
OWNER_MESSAGE = 55
BOLD = {"type": "bold", "offset": 0, "length": 4}
SOME_METHOD = SendMessage(chat_id=1, text="x")


class ScriptedBot(Bot):
    """Records every call; answers from a script, else with a plausible success."""

    def __init__(self, *script: Exception | None) -> None:
        super().__init__(token=BOT_TOKEN)
        self.calls: list[TelegramMethod[Any]] = []
        self.script = list(script)
        self._next_id = 100

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        del request_timeout
        self.calls.append(method)
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        self._next_id += 1
        if isinstance(method, CopyMessage):
            return MessageId(message_id=self._next_id)
        return Message(
            message_id=self._next_id,
            date=datetime.now(UTC),
            chat=Chat(id=BUYER, type="private"),
        )


def _item(kind: DailyContentKind, **overrides: Any) -> DailyContent:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": 7,
        "status": DailyContentStatus.ACTIVE,
        "kind": kind,
        "source_chat_id": OWNER_CHAT,
        "source_message_id": OWNER_MESSAGE,
        "added_by": OWNER_CHAT,
        "created_at": now,
        "updated_at": now,
        "status_changed_at": now,
        "body": "Wow! a caption",
        "entities": (BOLD,),
    }
    if kind is not DailyContentKind.TEXT:
        values |= {"file_id": f"{kind.value}-file-id", "file_unique_id": f"{kind.value}-unique"}
    values.update(overrides)
    return DailyContent(**values)


# --------------------------------------------------------------------------- #
# One Telegram method per kind                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "method_type", "file_field"),
    [
        (DailyContentKind.PHOTO, SendPhoto, "photo"),
        (DailyContentKind.VIDEO, SendVideo, "video"),
        (DailyContentKind.ANIMATION, SendAnimation, "animation"),
        (DailyContentKind.DOCUMENT, SendDocument, "document"),
        (DailyContentKind.AUDIO, SendAudio, "audio"),
        (DailyContentKind.VOICE, SendVoice, "voice"),
    ],
)
async def test_media_goes_out_by_file_id_with_its_caption(
    kind: DailyContentKind,
    method_type: type[TelegramMethod[Message]],
    file_field: str,
) -> None:
    bot = ScriptedBot()

    message_id = await TelegramDailyContentSender(bot).send(
        BUYER,
        _item(kind),
        protect_content=True,
    )

    (call,) = bot.calls
    assert isinstance(call, method_type)
    assert getattr(call, file_field) == f"{kind.value}-file-id"
    assert getattr(call, "chat_id", None) == BUYER
    assert getattr(call, "caption", None) == "Wow! a caption"
    assert getattr(call, "caption_entities", None) == [MessageEntity.model_validate(BOLD)]
    # Formatting travels as entities: Telegram must not re-parse the caption.
    assert getattr(call, "parse_mode", "unset") is None
    assert getattr(call, "protect_content", None) is True
    assert message_id == 101


async def test_a_round_video_goes_out_without_a_caption() -> None:
    bot = ScriptedBot()

    await TelegramDailyContentSender(bot).send(
        BUYER,
        _item(DailyContentKind.VIDEO_NOTE),
        protect_content=False,
    )

    (call,) = bot.calls
    assert isinstance(call, SendVideoNote)
    assert call.video_note == "video_note-file-id"
    assert call.protect_content is False


async def test_text_goes_out_with_its_entities() -> None:
    bot = ScriptedBot()

    await TelegramDailyContentSender(bot).send(
        BUYER,
        _item(DailyContentKind.TEXT, body="Wow! text"),
        protect_content=True,
    )

    (call,) = bot.calls
    assert isinstance(call, SendMessage)
    assert call.text == "Wow! text"
    assert call.entities == [MessageEntity.model_validate(BOLD)]
    assert call.parse_mode is None


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            TelegramRetryAfter(method=SOME_METHOD, message="Too Many Requests", retry_after=42),
            DailySendTransientError,
        ),
        (
            TelegramForbiddenError(method=SOME_METHOD, message="Forbidden: bot was blocked"),
            DailySendBlockedError,
        ),
        (
            TelegramBadRequest(method=SOME_METHOD, message="Bad Request: chat not found"),
            DailySendBlockedError,
        ),
        (
            TelegramBadRequest(method=SOME_METHOD, message="Bad Request: user is deactivated"),
            DailySendBlockedError,
        ),
        (
            TelegramBadRequest(
                method=SOME_METHOD,
                message="Bad Request: wrong file identifier/HTTP URL specified",
            ),
            DailyContentRejectedError,
        ),
        (
            TelegramBadRequest(method=SOME_METHOD, message="Bad Request: message is too long"),
            DailyContentRejectedError,
        ),
        (
            TelegramBadRequest(
                method=SOME_METHOD,
                message="Bad Request: can't parse entities: unclosed tag",
            ),
            DailyContentRejectedError,
        ),
        (
            TelegramEntityTooLarge(method=SOME_METHOD, message="Request Entity Too Large"),
            DailyContentRejectedError,
        ),
        (
            TelegramBadRequest(method=SOME_METHOD, message="Bad Request: something new"),
            DailySendRejectedError,
        ),
        (
            TelegramServerError(method=SOME_METHOD, message="Internal Server Error"),
            DailySendTransientError,
        ),
        (
            TelegramNetworkError(method=SOME_METHOD, message="Request timeout error"),
            DailySendTransientError,
        ),
        (
            TelegramUnauthorizedError(method=SOME_METHOD, message="Unauthorized"),
            DailySendTransientError,
        ),
        (TelegramAPIError(method=SOME_METHOD, message="Who knows"), DailySendTransientError),
        (ConnectionResetError("reset by peer"), DailySendTransientError),
    ],
    ids=[
        "retry-after",
        "forbidden",
        "chat-not-found",
        "deactivated",
        "wrong-file-id",
        "too-long",
        "bad-entities",
        "entity-too-large",
        "unknown-400",
        "server-error",
        "timeout",
        "unauthorized",
        "unknown-api-error",
        "not-telegram",
    ],
)
def test_every_failure_is_classified(error: Exception, expected: type[Exception]) -> None:
    classified = to_daily_send_error(error)
    assert type(classified) is expected


def test_flood_control_keeps_telegrams_hint() -> None:
    classified = to_daily_send_error(
        TelegramRetryAfter(method=SOME_METHOD, message="Too Many Requests", retry_after=42)
    )
    assert isinstance(classified, DailySendTransientError)
    assert classified.retry_after == 42


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            TelegramForbiddenError(method=SOME_METHOD, message="Forbidden: bot was blocked"),
            DailySendBlockedError,
        ),
        (
            TelegramNetworkError(method=SOME_METHOD, message="Request timeout error"),
            DailySendTransientError,
        ),
        (
            TelegramBadRequest(method=SOME_METHOD, message="Bad Request: something new"),
            DailySendRejectedError,
        ),
    ],
    ids=["blocked", "timeout", "rejected"],
)
async def test_only_a_refused_item_falls_back_to_a_copy(
    error: Exception,
    expected: type[Exception],
) -> None:
    bot = ScriptedBot(error)

    with pytest.raises(expected):
        await TelegramDailyContentSender(bot).send(
            BUYER,
            _item(DailyContentKind.VIDEO),
            protect_content=True,
        )

    assert len(bot.calls) == 1  # no copy attempted


# --------------------------------------------------------------------------- #
# The fallback to the owner's message                                         #
# --------------------------------------------------------------------------- #

WRONG_FILE = TelegramBadRequest(
    method=SOME_METHOD,
    message="Bad Request: wrong file identifier/HTTP URL specified",
)


async def test_a_refused_file_is_sent_as_a_copy_of_the_owners_message() -> None:
    bot = ScriptedBot(WRONG_FILE)

    message_id = await TelegramDailyContentSender(bot).send(
        BUYER,
        _item(DailyContentKind.VIDEO),
        protect_content=True,
    )

    first, copy = bot.calls
    assert isinstance(first, SendVideo)
    assert isinstance(copy, CopyMessage)
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (
        BUYER,
        OWNER_CHAT,
        OWNER_MESSAGE,
    )
    assert copy.protect_content is True
    assert message_id == 101


async def test_a_refused_copy_reports_the_item_as_refused() -> None:
    gone = TelegramBadRequest(method=SOME_METHOD, message="Bad Request: message to copy not found")
    bot = ScriptedBot(WRONG_FILE, gone)

    with pytest.raises(DailyContentRejectedError, match="wrong file identifier"):
        await TelegramDailyContentSender(bot).send(
            BUYER,
            _item(DailyContentKind.PHOTO),
            protect_content=True,
        )
    assert len(bot.calls) == 2


@pytest.mark.parametrize(
    ("copy_error", "expected"),
    [
        (
            TelegramNetworkError(method=SOME_METHOD, message="Request timeout error"),
            DailySendTransientError,
        ),
        (
            TelegramForbiddenError(method=SOME_METHOD, message="Forbidden: bot was blocked"),
            DailySendBlockedError,
        ),
    ],
    ids=["copy-timeout", "copy-blocked"],
)
async def test_a_copy_that_fails_otherwise_says_how(
    copy_error: Exception,
    expected: type[Exception],
) -> None:
    bot = ScriptedBot(WRONG_FILE, copy_error)

    with pytest.raises(expected):
        await TelegramDailyContentSender(bot).send(
            BUYER,
            _item(DailyContentKind.DOCUMENT),
            protect_content=True,
        )


async def test_an_item_that_cannot_even_be_built_is_copied_instead() -> None:
    """Stored entities Telegram's own models refuse: the item, not the chat."""
    bot = ScriptedBot()
    broken = _item(DailyContentKind.TEXT, entities=({"type": "bold", "offset": "x"},))

    message_id = await TelegramDailyContentSender(bot).send(BUYER, broken, protect_content=True)

    (copy,) = bot.calls
    assert isinstance(copy, CopyMessage)
    assert message_id == 101
