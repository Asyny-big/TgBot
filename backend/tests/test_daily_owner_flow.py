"""The Daily Content bot from the owner's side, end to end (PostgreSQL + Redis).

The owner sends and forwards content, presses buttons and asks for cards; the
real dispatcher, library service, repositories and Telegram sender handle it.
Only Telegram itself is a recording double.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    MessageOriginChannel,
    Sticker,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.daily_bot import texts
from app.daily_bot.keyboards import LibraryButton, LibraryCallback
from app.domain.enums import (
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
)
from app.infrastructure.db.repositories.daily import SqlAlchemyDailyContentRepository
from tests.concurrency import all_at_once
from tests.daily_bot_harness import (
    Clock,
    DailyHarness,
    bot_message,
    build_daily_harness,
    buyer,
    callback_update,
    owner_message,
    photo_sizes,
    seed_buyer,
    update_of,
    video,
)

if TYPE_CHECKING:
    from aiogram.types import Update

    from app.domain.entities import DailyContent, DailyDelivery
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

OWNER = 9700
BUYER = 9701
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
OWNER_USER = buyer(OWNER, username="owner")

Factory = Callable[..., DailyHarness]


@pytest.fixture
async def make_daily(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> AsyncIterator[Factory]:
    """Build a Daily bot over the live stores; ``settings`` override DAILY_*."""
    built: list[DailyHarness] = []

    def build(**settings: Any) -> DailyHarness:
        harness = build_daily_harness(
            live_uow_factory,
            live_locks,
            clock=Clock(T0),
            owner=OWNER,
            default_user=OWNER,
            **settings,
        )
        built.append(harness)
        return harness

    yield build
    for harness in built:
        await harness.close()


@pytest.fixture
def daily(make_daily: Factory) -> DailyHarness:
    return make_daily()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _from_owner(**fields: Any) -> Message:
    return owner_message(user=OWNER_USER, **fields)


def _press(
    button: LibraryButton,
    content_id: int = 0,
    *,
    user_id: int = OWNER,
    under: Message | None = None,
) -> Update:
    return callback_update(
        user=buyer(user_id),
        data=LibraryCallback(button=button, content_id=content_id).pack(),
        message=under or bot_message(chat_id=OWNER, text="…"),
    )


async def _items(daily: DailyHarness) -> list[DailyContent]:
    async with daily.uow_factory() as uow:
        rows = await uow.session.execute(text("SELECT id FROM daily_content ORDER BY id"))
        found = [await uow.daily_content.get(row[0]) for row in rows]
    return [item for item in found if item is not None]


async def _item(daily: DailyHarness, content_id: int) -> DailyContent:
    async with daily.uow_factory() as uow:
        found = await uow.daily_content.get(content_id)
    assert found is not None
    return found


async def _deliveries(daily: DailyHarness) -> list[DailyDelivery]:
    async with daily.uow_factory() as uow:
        rows = await uow.session.execute(text("SELECT id FROM daily_deliveries ORDER BY id"))
        found = [await uow.daily_deliveries.get(row[0]) for row in rows]
    return [delivery for delivery in found if delivery is not None]


def _replies(daily: DailyHarness, chat_id: int = OWNER) -> list[SendMessage]:
    return [call for call in daily.bot.sent_to(chat_id) if isinstance(call, SendMessage)]


def _buttons(markup: Any) -> list[list[tuple[str, str | None]]]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [
        [(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard
    ]


ACTIVE_CARD_BUTTONS = [
    [("🚫 Отключить", "dc:disable:1"), ("🗄 В архив", "dc:archive:1")],
    [("👁 Показать", "dc:show:1")],
]


# --------------------------------------------------------------------------- #
# Media                                                                       #
# --------------------------------------------------------------------------- #


async def test_the_owners_photo_is_added_at_once(daily: DailyHarness) -> None:
    sent = _from_owner(photo=photo_sizes("first"), caption="Первое")

    await daily.feed(update_of(sent))

    (item,) = await _items(daily)
    assert item.kind is DailyContentKind.PHOTO
    assert item.status is DailyContentStatus.ACTIVE
    assert item.file_id == "first-1280"
    assert item.body == "Первое"
    assert (item.source_chat_id, item.source_message_id, item.added_by) == (
        OWNER,
        sent.message_id,
        OWNER,
    )
    (reply,) = _replies(daily)
    assert reply.text == "✅ Добавлено #1 · фото · активных: 1"
    assert _buttons(reply.reply_markup) == [
        [("↩️ Отменить", "dc:undo:1"), ("🚫 Отключить", "dc:disable:1")]
    ]


async def test_a_forwarded_video_is_sourced_in_the_owners_chat(daily: DailyHarness) -> None:
    sent = _from_owner(
        video=video("clip"),
        caption="Ролик",
        forward_origin=MessageOriginChannel(
            date=T0,
            chat=Chat(id=-100777, type="channel", title="Канал"),
            message_id=4242,
        ),
    )

    await daily.feed(update_of(sent))

    (item,) = await _items(daily)
    assert item.kind is DailyContentKind.VIDEO
    assert (item.source_chat_id, item.source_message_id) == (OWNER, sent.message_id)
    assert _replies(daily)[-1].text == "✅ Добавлено #1 · видео 0:45 · активных: 1"


async def test_the_same_file_again_is_a_duplicate(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(video=video("clip"))))

    await daily.feed(update_of(_from_owner(video=video("clip"), caption="Другая подпись")))

    assert len(await _items(daily)) == 1
    reply = _replies(daily)[-1]
    assert reply.text == "⚠️ Уже есть #1 · видео 0:45 · активен"
    assert _buttons(reply.reply_markup) == ACTIVE_CARD_BUTTONS


async def test_a_replayed_update_adds_nothing_twice(daily: DailyHarness) -> None:
    sent = _from_owner(video=video("clip"))

    await daily.feed(update_of(sent))
    await daily.feed(update_of(sent))

    assert len(await _items(daily)) == 1
    assert [reply.text for reply in _replies(daily)] == [
        "✅ Добавлено #1 · видео 0:45 · активных: 1",
        "✅ Уже добавлено #1 · видео 0:45 · активен",
    ]


async def test_a_ten_file_album_is_added_whole_and_answered_once(make_daily: Factory) -> None:
    """Telegram delivers an album as ten messages at once. The owner is never
    throttled, so all ten reach the library — with a real throttling window."""
    daily = make_daily(throttle_seconds=1.5)
    album = [
        _from_owner(photo=photo_sizes(f"album-{number}"), media_group_id="album-1")
        for number in range(10)
    ]

    results = await all_at_once(*(daily.feed(update_of(message)) for message in album))
    await daily.settle()

    assert [result for result in results if isinstance(result, BaseException)] == []
    items = await _items(daily)
    assert len(items) == 10
    assert {item.file_id for item in items} == {f"album-{number}-1280" for number in range(10)}
    assert daily.bot.texts_to(OWNER) == [
        "📦 Альбом: 10\n✅ Добавлено 10: #1, #2, #3, #4, #5, #6, #7, #8, #9, #10"
    ]


async def test_an_album_summary_tells_new_from_known_and_archived(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("known"))))
    await daily.feed(update_of(_from_owner(photo=photo_sizes("archived"))))
    await daily.feed(_press(LibraryButton.ARCHIVE, 2))
    before = len(daily.bot.texts_to(OWNER))
    album = [
        _from_owner(photo=photo_sizes(tag), media_group_id="album-2")
        for tag in ("known", "archived", "new-1", "new-2")
    ]

    await all_at_once(*(daily.feed(update_of(message)) for message in album))
    await daily.settle()

    assert len(await _items(daily)) == 4
    assert daily.bot.texts_to(OWNER)[before:] == [
        (
            "📦 Альбом: 4\n"
            "✅ Добавлено 2: #3, #4\n"
            "⚠️ Уже было 1: #1\n"
            "🗄 Было в архиве 1: #2 — вернуть через /item"
        )
    ]


# --------------------------------------------------------------------------- #
# Text, only by the button                                                    #
# --------------------------------------------------------------------------- #


async def test_a_text_is_added_only_when_the_owner_presses_add(daily: DailyHarness) -> None:
    note = _from_owner(
        text="Мысль дня",
        entities=[MessageEntity(type="bold", offset=0, length=5)],
    )

    await daily.feed(update_of(note))

    assert await _items(daily) == []
    (prompt,) = _replies(daily)
    assert prompt.text == "Добавить этот текст в библиотеку?"
    assert prompt.reply_parameters is not None
    assert prompt.reply_parameters.message_id == note.message_id
    assert _buttons(prompt.reply_markup) == [
        [("➕ Добавить", "dc:add_text:0"), ("✖️ Не добавлять", "dc:skip_text:0")]
    ]

    under = bot_message(chat_id=OWNER, text=texts.TEXT_PROMPT, reply_to=note)
    await daily.feed(_press(LibraryButton.ADD_TEXT, under=under))

    (item,) = await _items(daily)
    assert item.kind is DailyContentKind.TEXT
    assert item.body == "Мысль дня"
    assert item.entities == ({"type": "bold", "offset": 0, "length": 5},)
    assert (item.source_chat_id, item.source_message_id) == (OWNER, note.message_id)
    assert daily.bot.edits()[-1].text == "✅ Добавлено #1 · текст · активных: 1"
    assert daily.bot.answers()[-1].text is None

    await daily.feed(_press(LibraryButton.ADD_TEXT, under=under))

    assert len(await _items(daily)) == 1
    assert daily.bot.edits()[-1].text == "✅ Уже добавлено #1 · текст · активен"


async def test_not_adding_a_text_adds_nothing(daily: DailyHarness) -> None:
    note = _from_owner(text="Просто заметка")
    await daily.feed(update_of(note))

    under = bot_message(chat_id=OWNER, text=texts.TEXT_PROMPT, reply_to=note)
    await daily.feed(_press(LibraryButton.SKIP_TEXT, under=under))

    assert await _items(daily) == []
    assert daily.bot.edits()[-1].text == "Текст не добавлен."


async def test_the_same_text_again_is_a_duplicate(daily: DailyHarness) -> None:
    for _ in range(2):
        note = _from_owner(text="Мысль дня")
        await daily.feed(update_of(note))
        under = bot_message(chat_id=OWNER, text=texts.TEXT_PROMPT, reply_to=note)
        await daily.feed(_press(LibraryButton.ADD_TEXT, under=under))

    assert len(await _items(daily)) == 1
    assert daily.bot.edits()[-1].text == "⚠️ Уже есть #1 · текст · активен"


async def test_add_without_the_text_it_answered_adds_nothing(daily: DailyHarness) -> None:
    await daily.feed(_press(LibraryButton.ADD_TEXT))

    assert await _items(daily) == []
    answer = daily.bot.answers()[-1]
    assert answer.text == "Не нашёл исходное сообщение. Отправьте текст ещё раз."
    assert answer.show_alert


# --------------------------------------------------------------------------- #
# Status: undo, restore, disable, enable                                      #
# --------------------------------------------------------------------------- #


async def test_undo_then_restore(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"), caption="Фото")))

    await daily.feed(_press(LibraryButton.UNDO, 1))

    undone = await _item(daily, 1)
    assert undone.status is DailyContentStatus.ARCHIVED
    assert undone.status_reason == "owner:undo"
    assert (daily.bot.edits()[-1].text or "").startswith("<b>#1 · фото</b> · в архиве")
    assert daily.bot.answers()[-1].text == "в архиве"

    # The same photo again: offered back, not added a second time.
    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))

    assert len(await _items(daily)) == 1
    offer = _replies(daily)[-1]
    assert offer.text == (
        "🗄 Этот материал уже был в библиотеке: #1 · фото, в архиве. Восстановить?"
    )
    assert _buttons(offer.reply_markup) == [
        [("♻️ Восстановить", "dc:restore:1")],
        [("👁 Показать", "dc:show:1")],
    ]

    await daily.feed(_press(LibraryButton.RESTORE, 1))

    restored = await _item(daily, 1)
    assert restored.status is DailyContentStatus.ACTIVE
    assert restored.status_reason == "owner:restore"
    assert daily.bot.answers()[-1].text == "активен"


async def test_disable_and_enable_from_the_card(daily: DailyHarness) -> None:
    sent = _from_owner(photo=photo_sizes("p"), caption="Первое <b>фото</b>")
    await daily.feed(update_of(sent))

    await daily.send("/item 1")

    card = _replies(daily)[-1]
    assert card.text is not None
    assert card.text.startswith("<b>#1 · фото</b> · активен\nДобавлен: ")
    assert "Подпись: Первое &lt;b&gt;фото&lt;/b&gt;" in card.text
    assert f"Источник: чат {OWNER}, сообщение {sent.message_id}" in card.text
    assert _buttons(card.reply_markup) == ACTIVE_CARD_BUTTONS

    await daily.feed(_press(LibraryButton.DISABLE, 1))
    disabled = await _item(daily, 1)
    assert disabled.status is DailyContentStatus.DISABLED
    assert disabled.status_reason == "owner:disable"
    assert "Причина: owner:disable" in (daily.bot.edits()[-1].text or "")

    await daily.feed(_press(LibraryButton.ENABLE, 1))
    assert (await _item(daily, 1)).status is DailyContentStatus.ACTIVE

    await daily.feed(_press(LibraryButton.ENABLE, 1))
    assert daily.bot.answers()[-1].text == "Без изменений."

    await daily.feed(_press(LibraryButton.ARCHIVE, 1))
    await daily.feed(_press(LibraryButton.ENABLE, 1))
    refused = daily.bot.answers()[-1]
    assert refused.text == "Из архива материал возвращается кнопкой «♻️ Восстановить»."
    assert refused.show_alert
    assert (await _item(daily, 1)).status is DailyContentStatus.ARCHIVED


async def test_stats(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("a"))))
    await daily.feed(update_of(_from_owner(photo=photo_sizes("b"))))
    await daily.feed(update_of(_from_owner(video=video("c"))))
    await daily.feed(_press(LibraryButton.DISABLE, 2))
    await daily.feed(_press(LibraryButton.ARCHIVE, 3))

    await daily.send("/stats")

    assert _replies(daily)[-1].text == (
        "📚 Библиотека\nАктивных: 1 · отключено: 1 · в архиве: 1\nАктивные по типам: фото 1"
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/item", "Номер материала: /item 121"),
        ("/item abc", "Номер материала: /item 121"),
        ("/item 99", "Материал #99 не найден."),
    ],
)
async def test_item_needs_an_existing_number(
    daily: DailyHarness,
    command: str,
    expected: str,
) -> None:
    await daily.send(command)

    assert _replies(daily)[-1].text == expected


async def test_item_accepts_a_hash(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))

    await daily.send("/item #1")

    assert (_replies(daily)[-1].text or "").startswith("<b>#1 · фото</b> · активен")


@pytest.mark.parametrize("command", ["/start", "/help", "/status", "/whatever"])
async def test_the_owner_gets_the_owners_help(daily: DailyHarness, command: str) -> None:
    await daily.send(command)

    assert daily.bot.texts_to(OWNER) == [texts.OWNER_HELP]
    assert await _items(daily) == []


async def test_show_sends_the_item_to_the_owner_as_buyers_get_it(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"), caption="Фото")))

    await daily.feed(_press(LibraryButton.SHOW, 1))

    (preview,) = daily.bot.items_to(OWNER)
    assert isinstance(preview, SendPhoto)
    assert preview.photo == "p-1280"
    assert preview.caption == "Фото"
    assert preview.protect_content is True
    assert daily.bot.answers()[-1].text is None
    assert await _deliveries(daily) == []


# --------------------------------------------------------------------------- #
# Who cannot change the library                                               #
# --------------------------------------------------------------------------- #


async def test_an_ordinary_user_never_creates_content(daily: DailyHarness) -> None:
    await seed_buyer(daily.uow_factory, BUYER, completed=20)
    stranger = buyer(424242)

    await daily.feed(update_of(owner_message(user=stranger, photo=photo_sizes("x"))))
    await daily.feed(update_of(owner_message(user=buyer(BUYER), video=video("y"))))
    await daily.feed(update_of(owner_message(user=stranger, text="/stats")))

    assert await _items(daily) == []
    assert daily.bot.texts_to(424242) == [
        "Daily-контент открывается с 15-й покупки. Сейчас у вас 0 из 15.",
        "Daily-контент открывается с 15-й покупки. Сейчас у вас 0 из 15.",
    ]


async def test_a_forged_button_changes_nothing_and_says_nothing(daily: DailyHarness) -> None:
    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))
    edits_before = len(daily.bot.edits())

    for button in (LibraryButton.ARCHIVE, LibraryButton.DISABLE, LibraryButton.SHOW):
        await daily.feed(_press(button, 1, user_id=BUYER))

    assert (await _item(daily, 1)).status is DailyContentStatus.ACTIVE
    assert [answer.text for answer in daily.bot.answers()] == [None, None, None]
    assert len(daily.bot.edits()) == edits_before
    assert daily.bot.items_to(BUYER) == []


async def test_with_no_owner_configured_nobody_fills_the_library(make_daily: Factory) -> None:
    daily = make_daily(admin_telegram_ids=())

    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))
    await daily.send("/stats")

    assert await _items(daily) == []
    assert texts.OWNER_HELP not in daily.bot.texts_to(OWNER)


async def test_content_posted_in_a_group_is_ignored(daily: DailyHarness) -> None:
    group = -100500

    await daily.feed(
        update_of(
            _from_owner(photo=photo_sizes("p"), chat_type="supergroup", chat_id=group),
        )
    )

    assert await _items(daily) == []
    assert daily.bot.sent_to(group) == []
    assert daily.bot.sent_to(OWNER) == []


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
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
            (
                "⛔ Не поддерживается: стикер. "
                "Можно: текст, фото, видео, GIF, документ, аудио, голосовое, кружок."
            ),
        ),
        (
            {"photo": photo_sizes("p"), "has_protected_content": True},
            "⛔ Это сообщение защищено от пересылки — его нельзя добавить.",
        ),
    ],
    ids=["sticker", "protected"],
)
async def test_what_the_library_cannot_take_is_refused_with_a_reason(
    daily: DailyHarness,
    fields: dict[str, Any],
    expected: str,
) -> None:
    await daily.feed(update_of(_from_owner(**fields)))

    assert await _items(daily) == []
    assert daily.bot.texts_to(OWNER) == [expected]


# --------------------------------------------------------------------------- #
# Failures and side effects                                                   #
# --------------------------------------------------------------------------- #


async def test_a_database_failure_is_reported_and_sending_again_is_safe(
    daily: DailyHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lost_connection(*_: Any, **__: Any) -> Any:
        statement = "INSERT INTO daily_content"
        raise OperationalError(statement, {}, ConnectionResetError("reset"))

    sent = _from_owner(photo=photo_sizes("p"))
    with monkeypatch.context() as patched:
        patched.setattr(SqlAlchemyDailyContentRepository, "add", lost_connection)
        await daily.feed(update_of(sent))

    assert await _items(daily) == []
    assert daily.bot.texts_to(OWNER) == [
        "❌ Не сохранено (ошибка базы). Перешлите сообщение ещё раз."
    ]

    await daily.feed(update_of(sent))

    assert len(await _items(daily)) == 1
    assert daily.bot.texts_to(OWNER)[-1] == "✅ Добавлено #1 · фото · активных: 1"


async def test_adding_content_delivers_nothing_to_anybody(daily: DailyHarness) -> None:
    """A buyer waits with nothing to receive; new content must still wait for
    the delivery engine — the owner's intake never sends or plans anything."""
    await seed_buyer(daily.uow_factory, BUYER)
    await daily.send("/start", BUYER)
    (waiting,) = await _deliveries(daily)
    assert waiting.status is DailyDeliveryStatus.NO_CONTENT
    daily.clock.advance(hours=1)  # the delivery is due again by now

    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))
    await daily.feed(update_of(_from_owner(video=video("v"))))

    assert daily.bot.items_to(BUYER) == []
    (still_waiting,) = await _deliveries(daily)
    assert still_waiting == waiting


async def test_the_library_fills_while_daily_content_is_off(make_daily: Factory) -> None:
    daily = make_daily(enabled=False)
    await seed_buyer(daily.uow_factory, BUYER)

    await daily.feed(update_of(_from_owner(photo=photo_sizes("p"))))
    await daily.send("/start", BUYER)

    assert len(await _items(daily)) == 1
    assert daily.bot.texts_to(OWNER) == ["✅ Добавлено #1 · фото · активных: 1"]
    assert daily.bot.texts_to(BUYER) == ["Раздел скоро откроется."]
    assert daily.bot.items_to(BUYER) == []
    assert await _deliveries(daily) == []
