"""A buyer's content request in the Daily bot: a draft, then one request.

The draft lives in the bot's FSM storage — Redis in production, under a prefix
of its own — for half an hour after its last change, and only in the buyer's
private chat with the bot.

1. **"💡 Suggest content"** opens a draft for a buyer who may suggest: 20 or
   more completed purchases, no ban, within the limits. The button shows only
   then, and every step below checks again.
2. **Every message** — not a command, not a menu button — joins the draft,
   parsed exactly like the owner's content: up to ten, a text up to 2000
   characters. An album, or a burst of forwards, is serialised by the draft's
   lock so none of its messages is lost, and an album is answered once.
3. **"✅ Send"** turns the draft into a request, once: the button carries the
   draft's id, so a second tap, a retry after a failure, or a button left over
   from an old draft never makes a second request. The draft is forgotten in
   the same step that sent it.

The buyer is always ``from_user`` — Telegram's own account of who wrote.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.core.exceptions import LockBusyError
from app.core.logging import get_logger
from app.daily_bot import texts
from app.daily_bot.albums import DraftItem, DraftOutcome
from app.daily_bot.handlers.subscriber import contact, menu_for
from app.daily_bot.intake import Refusal, parse_owner_message
from app.daily_bot.keyboards import DraftButton, DraftCallback, draft_keyboard, menu_keyboard
from app.daily_bot.middlewares import DailyServices
from app.domain.commands import DailyContentDraft
from app.domain.daily_requests import ItemProblem, RequestAccess, item_problem
from app.domain.enums import DailyContentKind, DailySubscriptionStatus
from app.services.daily_requests import AccessResult, SubmitOutcome, SubmitResult

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery

logger = get_logger(__name__)

DRAFT_QUEUE_SECONDS: Final = 10.0
"""How long a message of a draft may wait for the one before it."""

_DRAFT_ID: Final = "draft_id"
_ITEMS: Final = "items"


class RequestDraft(StatesGroup):
    """The one state of a content request: a draft being filled."""

    collecting = State()


def draft_patience(data: dict[str, Any]) -> float:
    """For the throttle: a message of an open draft waits its turn."""
    return DRAFT_QUEUE_SECONDS if data.get("raw_state") == RequestDraft.collecting.state else 0.0


# --------------------------------------------------------------------------- #
# The draft in the FSM storage                                                #
# --------------------------------------------------------------------------- #


def _dump(draft: DailyContentDraft) -> dict[str, Any]:
    return {
        "kind": draft.kind.value,
        "source_chat_id": draft.source_chat_id,
        "source_message_id": draft.source_message_id,
        "added_by": draft.added_by,
        "body": draft.body,
        "entities": [dict(entity) for entity in draft.entities],
        "file_id": draft.file_id,
        "file_unique_id": draft.file_unique_id,
        "file_size": draft.file_size,
        "file_name": draft.file_name,
        "mime_type": draft.mime_type,
        "duration": draft.duration,
    }


def _load(raw: dict[str, Any]) -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind(raw["kind"]),
        source_chat_id=int(raw["source_chat_id"]),
        source_message_id=int(raw["source_message_id"]),
        added_by=int(raw["added_by"]),
        body=raw.get("body"),
        entities=tuple(raw.get("entities") or ()),
        file_id=raw.get("file_id"),
        file_unique_id=raw.get("file_unique_id"),
        file_size=raw.get("file_size"),
        file_name=raw.get("file_name"),
        mime_type=raw.get("mime_type"),
        duration=raw.get("duration"),
    )


async def _open_draft(state: FSMContext) -> tuple[str, list[dict[str, Any]]] | None:
    """The open draft's id and messages; ``None`` when there is none."""
    if await state.get_state() != RequestDraft.collecting.state:
        return None
    data = await state.get_data()
    draft_id, items = data.get(_DRAFT_ID), data.get(_ITEMS)
    if not isinstance(draft_id, str) or not isinstance(items, list):
        return None
    return draft_id, items


# --------------------------------------------------------------------------- #
# Starting                                                                    #
# --------------------------------------------------------------------------- #


async def handle_suggest(message: Message, daily: DailyServices, state: FSMContext) -> None:
    """ "💡 Suggest content": open a draft, or show the one that is open."""
    subscription = await contact(message, daily)
    if subscription is None:
        return
    user_id = subscription.user_id
    paused = subscription.status is DailySubscriptionStatus.PAUSED
    try:
        async with daily.requests.draft_lock(user_id):
            access = await daily.requests.access(user_id)
            if not access.allowed:
                if not _keeps_draft(access):
                    await state.clear()
                await _refuse(message, access, paused=paused)
                return
            draft = await _open_draft(state)
            if draft is not None:
                draft_id, items = draft
                await message.answer(
                    texts.draft_exists(len(items)),
                    reply_markup=draft_keyboard(draft_id),
                )
                return
            draft_id = uuid4().hex
            await state.set_state(RequestDraft.collecting)
            await state.set_data({_DRAFT_ID: draft_id, _ITEMS: []})
    except LockBusyError:
        await message.answer(texts.TOO_FAST)
        return
    logger.info("daily_request_draft_started", user_id=user_id)
    await message.answer(texts.DRAFT_STARTED, reply_markup=draft_keyboard(draft_id))


# --------------------------------------------------------------------------- #
# Filling                                                                     #
# --------------------------------------------------------------------------- #


def is_draft_message(message: Message) -> bool:
    """Not a command and not a menu button: those keep working while drafting."""
    text = message.text
    return not (text and (text.startswith("/") or text in texts.MENU_BUTTONS))


async def handle_draft_message(
    message: Message,
    daily: DailyServices,
    state: FSMContext,
) -> None:
    """A message for the open draft."""
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return
    group = message.media_group_id
    if group is not None:
        daily.draft_albums.begin(message.chat.id, group)
    try:
        item = await _add(message, daily, state, user.id)
    except LockBusyError:
        item = DraftItem(outcome=DraftOutcome.GONE, notice=texts.TOO_FAST)
    except Exception:
        if group is not None and message.bot is not None:
            # The album's answer must not wait for this message forever.
            failed = DraftItem(outcome=DraftOutcome.GONE, notice=texts.REQUEST_FAILED)
            daily.draft_albums.finish(message.chat.id, group, failed, bot=message.bot)
        raise
    if group is not None and message.bot is not None:
        daily.draft_albums.finish(message.chat.id, group, item, bot=message.bot)
        return
    await _answer_item(message, item)


async def _add(
    message: Message,
    daily: DailyServices,
    state: FSMContext,
    user_id: int,
) -> DraftItem:
    """Add the message under the draft's lock; what became of it."""
    parsed = parse_owner_message(message)
    async with daily.requests.draft_lock(user_id):
        draft = await _open_draft(state)
        if draft is None:
            return DraftItem(outcome=DraftOutcome.GONE, notice=texts.DRAFT_EXPIRED)
        draft_id, items = draft
        access = await daily.requests.access(user_id)
        if not access.allowed:
            await state.clear()
            return DraftItem(outcome=DraftOutcome.GONE, notice=_refusal_text(access))
        if isinstance(parsed, Refusal):
            return DraftItem(
                outcome=DraftOutcome.REFUSED,
                count=len(items),
                draft_id=draft_id,
                notice=texts.refused(parsed),
            )
        if any(
            raw.get("source_chat_id") == parsed.source_chat_id
            and raw.get("source_message_id") == parsed.source_message_id
            for raw in items
        ):  # the same update delivered twice
            return DraftItem(outcome=DraftOutcome.ADDED, count=len(items), draft_id=draft_id)
        problem = item_problem(parsed, items_so_far=len(items))
        if problem is not None:
            outcome = (
                DraftOutcome.FULL if problem is ItemProblem.DRAFT_FULL else DraftOutcome.TOO_LONG
            )
            return DraftItem(outcome=outcome, count=len(items), draft_id=draft_id)
        items.append(_dump(parsed))
        # The chat's order, whatever order concurrent messages were handled in.
        items.sort(key=lambda raw: int(raw["source_message_id"]))
        await state.set_state(RequestDraft.collecting)  # a change keeps the draft alive
        await state.set_data({_DRAFT_ID: draft_id, _ITEMS: items})
    return DraftItem(outcome=DraftOutcome.ADDED, count=len(items), draft_id=draft_id)


async def _answer_item(message: Message, item: DraftItem) -> None:
    keyboard = draft_keyboard(item.draft_id) if item.draft_id is not None else None
    match item.outcome:
        case DraftOutcome.ADDED:
            await message.answer(texts.draft_added(item.count), reply_markup=keyboard)
        case DraftOutcome.FULL:
            await message.answer(texts.DRAFT_FULL, reply_markup=keyboard)
        case DraftOutcome.TOO_LONG:
            await message.answer(texts.DRAFT_TEXT_TOO_LONG, reply_markup=keyboard)
        case DraftOutcome.REFUSED | DraftOutcome.GONE:
            await message.answer(item.notice or texts.DRAFT_EXPIRED, reply_markup=keyboard)


# --------------------------------------------------------------------------- #
# Sending and cancelling                                                      #
# --------------------------------------------------------------------------- #


async def handle_send(
    callback: CallbackQuery,
    callback_data: DraftCallback,
    daily: DailyServices,
    state: FSMContext,
) -> None:
    """ "✅ Send": the draft this button was shown under becomes a request, once."""
    user_id = callback.from_user.id
    try:
        client_draft_id = UUID(hex=callback_data.draft)
    except ValueError:
        await callback.answer(texts.DRAFT_EXPIRED, show_alert=True)
        return
    result: SubmitResult | None = None
    try:
        async with daily.requests.draft_lock(user_id):
            draft = await _open_draft(state)
            if draft is None or draft[0] != callback_data.draft:
                # Not the open draft: it was sent already, or it is gone.
                sent = await daily.requests.find_submitted(user_id, client_draft_id)
                answer = texts.REQUEST_ALREADY_SENT if sent is not None else texts.DRAFT_EXPIRED
                await callback.answer(answer, show_alert=sent is None)
                return
            try:
                items = [_load(raw) for raw in draft[1]]
            except (KeyError, TypeError, ValueError):
                logger.warning("daily_request_draft_unreadable", user_id=user_id)
                await state.clear()
                await callback.answer(texts.DRAFT_EXPIRED, show_alert=True)
                return
            if not items:
                await callback.answer(texts.DRAFT_EMPTY, show_alert=True)
                return
            result = await daily.requests.submit(
                user_id,
                client_draft_id=client_draft_id,
                items=items,
            )
            if result.outcome in (SubmitOutcome.CREATED, SubmitOutcome.ALREADY_SUBMITTED):
                # The request exists: the draft is done, in this very step.
                await state.clear()
            elif result.outcome in (SubmitOutcome.REFUSED, SubmitOutcome.INVALID) and (
                result.access is None or not _keeps_draft(result.access)
            ):
                await state.clear()
    except LockBusyError:
        await callback.answer(texts.REQUEST_BUSY)
        return
    if result is not None:
        await _report_submit(callback, daily, result)


def _keeps_draft(access: AccessResult) -> bool:
    """A limit passes by itself: the draft waits for it. Anything else ends it."""
    return access.access in (RequestAccess.TOO_MANY_OPEN, RequestAccess.RATE_LIMITED)


async def _report_submit(
    callback: CallbackQuery,
    daily: DailyServices,
    result: SubmitResult,
) -> None:
    user_id = callback.from_user.id
    match result.outcome:
        case SubmitOutcome.CREATED | SubmitOutcome.ALREADY_SUBMITTED:
            await callback.answer()
            await _drop_buttons(callback)
            await _say(
                callback,
                texts.REQUEST_THANKS,
                reply_markup=await menu_for(daily, user_id),
            )
        case SubmitOutcome.BUSY:
            await callback.answer(texts.REQUEST_BUSY)
        case SubmitOutcome.EMPTY:
            await callback.answer(texts.DRAFT_EMPTY, show_alert=True)
        case SubmitOutcome.REFUSED if result.access is not None:
            await callback.answer()
            await _say(
                callback,
                _refusal_text(result.access),
                reply_markup=await menu_for(daily, user_id),
            )
        case _:
            await callback.answer(texts.DRAFT_EXPIRED, show_alert=True)


async def handle_cancel_button(
    callback: CallbackQuery,
    callback_data: DraftCallback,
    daily: DailyServices,
    state: FSMContext,
) -> None:
    """ "✖️ Cancel" under the open draft."""
    user_id = callback.from_user.id
    try:
        async with daily.requests.draft_lock(user_id):
            draft = await _open_draft(state)
            if draft is None or draft[0] != callback_data.draft:
                await callback.answer(texts.DRAFT_EXPIRED)
                return
            await state.clear()
    except LockBusyError:
        await callback.answer(texts.REQUEST_BUSY)
        return
    await callback.answer()
    await _drop_buttons(callback)
    await _say(callback, texts.DRAFT_CANCELLED, reply_markup=await menu_for(daily, user_id))


async def handle_cancel_command(
    message: Message,
    daily: DailyServices,
    state: FSMContext,
) -> None:
    """``/cancel`` while a draft is open."""
    user = message.from_user
    if user is None:  # pragma: no cover
        return
    try:
        async with daily.requests.draft_lock(user.id):
            await state.clear()
    except LockBusyError:
        await message.answer(texts.TOO_FAST)
        return
    await message.answer(texts.DRAFT_CANCELLED, reply_markup=await menu_for(daily, user.id))


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _refusal_text(access: AccessResult) -> str:
    match access.access:
        case RequestAccess.NOT_ELIGIBLE:
            return texts.request_not_eligible(access.completed_purchases)
        case RequestAccess.BANNED:
            return texts.REQUEST_BANNED
        case RequestAccess.TOO_MANY_OPEN:
            return texts.REQUEST_TOO_MANY_OPEN
        case RequestAccess.RATE_LIMITED:
            return texts.REQUEST_RATE_LIMITED
        case _:
            return texts.UNAVAILABLE


async def _refuse(message: Message, access: AccessResult, *, paused: bool) -> None:
    """Why not — with the menu as it now is (the button gone, if it should be)."""
    await message.answer(
        _refusal_text(access),
        reply_markup=menu_keyboard(paused=paused, can_suggest=access.can_suggest),
    )


async def _drop_buttons(callback: CallbackQuery) -> None:
    """The draft's buttons are spent: take them off the message. Cosmetic."""
    if isinstance(callback.message, Message):
        with suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


async def _say(callback: CallbackQuery, text: str, **kwargs: Any) -> None:
    """A new message in the chat the button was pressed in."""
    bot = callback.bot
    if bot is None:  # pragma: no cover — dispatched callbacks carry their bot
        return
    await bot.send_message(chat_id=callback.from_user.id, text=text, **kwargs)


def build_router() -> Router:
    """A fresh router; registered before the buyer's general one."""
    router = Router(name="daily_requests")
    private = F.chat.type == "private"
    drafting = StateFilter(RequestDraft.collecting)
    router.message(F.text == texts.SUGGEST_BUTTON, private)(handle_suggest)
    router.message(Command("cancel"), drafting, private)(handle_cancel_command)
    router.message(drafting, private, is_draft_message)(handle_draft_message)
    router.callback_query(DraftCallback.filter(F.button == DraftButton.SEND))(handle_send)
    router.callback_query(DraftCallback.filter(F.button == DraftButton.CANCEL))(
        handle_cancel_button
    )
    return router


__all__ = ["DRAFT_QUEUE_SECONDS", "RequestDraft", "build_router", "draft_patience"]
