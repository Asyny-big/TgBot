"""The owner's side of the Daily Content bot: filling the library.

The owner's accounts (``DAILY_ADMIN_TELEGRAM_IDS``) never go through the
buyer's flow. Whatever they send or forward in their private chat with the bot
is content: media is added at once, a text only when the owner presses the
"Add" button under it — a stray note must not become an item. An album gets
one summary instead of an answer per file.

Every item can be switched off, archived and brought back from buttons under
the answers and under ``/item <id>``; ``/stats`` counts the library. Nothing is
deleted, and nothing here delivers anything to anybody.

This router is registered first and only for the owner's accounts, and
``DailyLibraryService`` checks the owner again on every call. With no owner
configured the router is empty and every message is a buyer's.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from app.core.logging import get_logger
from app.daily_bot import texts
from app.daily_bot.albums import AlbumItem, AlbumOutcome
from app.daily_bot.intake import Refusal, parse_owner_message
from app.daily_bot.keyboards import (
    LIBRARY_ACTIONS,
    LibraryButton,
    LibraryCallback,
    added_keyboard,
    card_keyboard,
    text_keyboard,
)
from app.daily_bot.middlewares import DailyServices
from app.domain.enums import DailyContentKind
from app.services.daily_library import (
    AddOutcome,
    AddResult,
    PreviewOutcome,
    StatusOutcome,
)

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

    from app.domain.commands import DailyContentDraft

logger = get_logger(__name__)

_ALBUM_OUTCOMES = {
    AddOutcome.ADDED: AlbumOutcome.ADDED,
    AddOutcome.ALREADY_ADDED: AlbumOutcome.EXISTING,
    AddOutcome.DUPLICATE: AlbumOutcome.EXISTING,
    AddOutcome.ARCHIVED_TWIN: AlbumOutcome.ARCHIVED,
    AddOutcome.NOT_OWNER: AlbumOutcome.FAILED,
}
_CALLBACK_ANSWER_LIMIT = 200


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


async def handle_help(message: Message) -> None:
    """``/start``, ``/help`` and any command the owner's menu does not know."""
    await message.answer(texts.OWNER_HELP)


async def handle_stats(message: Message, daily: DailyServices) -> None:
    """``/stats``: the library in numbers, and how long it lasts the buyers."""
    numbers = await daily.library.stats(_actor(message))
    if numbers is None:  # pragma: no cover — the router lets only the owner in
        return
    snapshot = await daily.stock.snapshot()
    await message.answer(texts.stats(numbers, snapshot.summary))


async def handle_item(message: Message, command: CommandObject, daily: DailyServices) -> None:
    """``/item 121``: one item's card, with what its status allows."""
    content_id = _item_number(command.args)
    if content_id is None:
        await message.answer(texts.ITEM_USAGE)
        return
    item = await daily.library.card(_actor(message), content_id)
    if item is None:
        await message.answer(texts.item_not_found(content_id))
        return
    await message.answer(texts.card(item), reply_markup=card_keyboard(item))


# --------------------------------------------------------------------------- #
# Content                                                                     #
# --------------------------------------------------------------------------- #


async def handle_content(message: Message, daily: DailyServices) -> None:
    """One message, not part of an album: media is added, a text is asked about."""
    parsed = parse_owner_message(message)
    if isinstance(parsed, Refusal):
        await message.answer(texts.refused(parsed))
        return
    if parsed.kind is DailyContentKind.TEXT:
        await message.reply(texts.TEXT_PROMPT, reply_markup=text_keyboard())
        return
    result = await _add(daily, _actor(message), parsed)
    if result is None:
        await message.answer(texts.NOT_SAVED)
        return
    text, markup = _added_view(result)
    await message.answer(text, reply_markup=markup)


async def handle_album_item(message: Message, daily: DailyServices, bot: Bot) -> None:
    """One file of an album: added at once, answered in the album's summary."""
    chat_id, group_id = message.chat.id, str(message.media_group_id)
    daily.albums.begin(chat_id, group_id)
    item = AlbumItem(AlbumOutcome.FAILED)
    try:
        parsed = parse_owner_message(message)
        if isinstance(parsed, Refusal):
            item = AlbumItem(AlbumOutcome.REFUSED)
        else:
            result = await _add(daily, _actor(message), parsed)
            if result is not None:
                content_id = result.item.id if result.item is not None else None
                item = AlbumItem(_ALBUM_OUTCOMES[result.outcome], content_id)
    finally:
        daily.albums.finish(chat_id, group_id, item, bot=bot)


async def _add(daily: DailyServices, actor_id: int, draft: DailyContentDraft) -> AddResult | None:
    """Add the item; ``None`` when it could not be saved.

    Telegram does not send an update again once it was handed over, so a
    failure must reach the owner: sending the message again is always safe.
    """
    try:
        return await daily.library.add(actor_id, draft)
    except Exception:
        logger.exception(
            "daily_content_not_saved",
            source_chat_id=draft.source_chat_id,
            source_message_id=draft.source_message_id,
        )
        return None


def _added_view(result: AddResult) -> tuple[str, InlineKeyboardMarkup | None]:
    item = result.item
    if item is None or result.outcome is AddOutcome.NOT_OWNER:  # pragma: no cover
        return texts.UNAVAILABLE, None
    match result.outcome:
        case AddOutcome.ADDED:
            return texts.added(item, active_total=result.active_total), added_keyboard(item)
        case AddOutcome.ALREADY_ADDED:
            return texts.already_added(item), card_keyboard(item)
        case AddOutcome.DUPLICATE:
            return texts.duplicate(item), card_keyboard(item)
        case _:
            return texts.archived_twin(item), card_keyboard(item)


# --------------------------------------------------------------------------- #
# Buttons                                                                     #
# --------------------------------------------------------------------------- #


async def handle_button(
    callback: CallbackQuery,
    callback_data: LibraryCallback,
    daily: DailyServices,
    bot: Bot,
) -> None:
    """Every owner's button. The service checks the owner once more."""
    button = callback_data.button
    if button is LibraryButton.ADD_TEXT:
        await _add_text(callback, daily, bot)
    elif button is LibraryButton.SKIP_TEXT:
        await _show(callback, bot, texts.TEXT_SKIPPED, None)
        await callback.answer()
    elif button is LibraryButton.SHOW:
        await _preview(callback, daily, callback_data.content_id)
    else:
        await _change_status(callback, daily, bot, callback_data)


async def _add_text(callback: CallbackQuery, daily: DailyServices, bot: Bot) -> None:
    """The "Add" button: the text is the owner's message the question replies to."""
    prompt = callback.message
    original = prompt.reply_to_message if isinstance(prompt, Message) else None
    if original is None or original.from_user is None:
        await callback.answer(texts.TEXT_GONE, show_alert=True)
        return
    if original.from_user.id != callback.from_user.id:  # pragma: no cover — one owner's chat
        await callback.answer(texts.UNAVAILABLE, show_alert=True)
        return
    parsed = parse_owner_message(original)
    if isinstance(parsed, Refusal) or parsed.kind is not DailyContentKind.TEXT:
        await callback.answer(texts.TEXT_GONE, show_alert=True)
        return
    result = await _add(daily, callback.from_user.id, parsed)
    if result is None:
        await callback.answer(texts.NOT_SAVED, show_alert=True)
        return
    if result.outcome is AddOutcome.NOT_OWNER:
        await callback.answer(texts.UNAVAILABLE, show_alert=True)
        return
    text, markup = _added_view(result)
    await _show(callback, bot, text, markup)
    await callback.answer()


async def _change_status(
    callback: CallbackQuery,
    daily: DailyServices,
    bot: Bot,
    callback_data: LibraryCallback,
) -> None:
    action = LIBRARY_ACTIONS[callback_data.button]
    result = await daily.library.change_status(
        callback.from_user.id,
        callback_data.content_id,
        action,
    )
    match result.outcome:
        case StatusOutcome.CHANGED if result.item is not None:
            await _show(callback, bot, texts.card(result.item), card_keyboard(result.item))
            await callback.answer(texts.STATUS_LABELS[result.item.status])
        case StatusOutcome.UNCHANGED:
            await callback.answer(texts.UNCHANGED)
        case StatusOutcome.NOT_ALLOWED:
            await callback.answer(texts.NOT_ALLOWED, show_alert=True)
        case StatusOutcome.DUPLICATE if result.twin is not None:
            await callback.answer(texts.restore_blocked(result.twin), show_alert=True)
        case StatusOutcome.NOT_FOUND:
            await callback.answer(texts.item_not_found(callback_data.content_id))
        case _:
            await callback.answer(texts.UNAVAILABLE, show_alert=True)


async def _preview(callback: CallbackQuery, daily: DailyServices, content_id: int) -> None:
    """The "Show" button: the item, sent to the owner exactly as a buyer gets it."""
    result = await daily.library.preview(callback.from_user.id, content_id)
    match result.outcome:
        case PreviewOutcome.SENT:
            await callback.answer()
        case PreviewOutcome.FAILED:
            failure = texts.preview_failed(result.error or "")
            await callback.answer(failure[:_CALLBACK_ANSWER_LIMIT], show_alert=True)
        case PreviewOutcome.NOT_FOUND:
            await callback.answer(texts.item_not_found(content_id))
        case _:
            await callback.answer(texts.UNAVAILABLE, show_alert=True)


async def _show(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Replace the message under the button; send a new one if it cannot be edited."""
    message = callback.message
    if isinstance(message, Message):
        with suppress(TelegramBadRequest):
            await message.edit_text(text, reply_markup=markup)
            return
    with suppress(TelegramAPIError):
        await bot.send_message(chat_id=callback.from_user.id, text=text, reply_markup=markup)


# --------------------------------------------------------------------------- #
# Helpers and the router                                                      #
# --------------------------------------------------------------------------- #


def _actor(message: Message) -> int:
    return message.from_user.id if message.from_user is not None else message.chat.id


def _item_number(args: str | None) -> int | None:
    """``"121"`` or ``"#121"`` → ``121``; anything else → ``None``."""
    raw = (args or "").strip().removeprefix("#")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def build_router(owner_ids: tuple[int, ...]) -> Router:
    """The owner's router; registered before the buyer's. Empty with no owner."""
    router = Router(name="daily_owner")
    if not owner_ids:
        return router
    owner = F.from_user.id.in_(owner_ids)
    private = F.chat.type == ChatType.PRIVATE
    router.message(owner, private, CommandStart())(handle_help)
    router.message(owner, private, Command("help"))(handle_help)
    router.message(owner, private, Command("stats"))(handle_stats)
    router.message(owner, private, Command("item"))(handle_item)
    router.message(owner, private, F.text.startswith("/"))(handle_help)
    router.message(owner, private, F.media_group_id)(handle_album_item)
    router.message(owner, private)(handle_content)
    router.callback_query(
        owner,
        F.message.chat.type == ChatType.PRIVATE,
        LibraryCallback.filter(),
    )(handle_button)
    return router


__all__ = ["build_router"]
