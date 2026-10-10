"""The buyer's side of the Daily Content bot.

Every private message — a command, a button, anything — is first a *contact*:
``DailySubscriptionService.first_contact`` decides whether it activates the
buyer. An activating contact gets the welcome and then the first item, and
nothing else; any later message does what it asks: status, pause, resume, help.

The buyer is always ``message.from_user`` — Telegram's own account of who wrote.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart

from app.core.logging import get_logger
from app.daily_bot import texts
from app.daily_bot.keyboards import menu_keyboard
from app.daily_bot.middlewares import DailyServices
from app.domain.daily import DeliveryStep
from app.domain.enums import DailySubscriptionStatus
from app.services.daily_subscriptions import ContactOutcome, ContactResult

if TYPE_CHECKING:
    from aiogram.types import Message

    from app.domain.entities import DailySubscription

logger = get_logger(__name__)


async def handle_start(message: Message, daily: DailyServices) -> None:
    """``/start``: activates on the first eligible contact, else shows the status."""
    if await _contact(message, daily) is not None:
        await _show_status(message, daily)


async def handle_status(message: Message, daily: DailyServices) -> None:
    """``/status`` or "📊 My status"."""
    if await _contact(message, daily) is not None:
        await _show_status(message, daily)


async def handle_pause(message: Message, daily: DailyServices) -> None:
    """``/pause`` or "⏸ Pause"."""
    subscription = await _contact(message, daily)
    if subscription is None:
        return
    if await daily.subscriptions.pause(subscription.user_id) is not None:
        await message.answer(texts.PAUSED, reply_markup=menu_keyboard(paused=True))


async def handle_resume(message: Message, daily: DailyServices) -> None:
    """``/resume`` or "▶️ Resume"."""
    subscription = await _contact(message, daily)
    if subscription is None:
        return
    if await daily.subscriptions.resume(subscription.user_id) is not None:
        await message.answer(texts.RESUMED, reply_markup=menu_keyboard(paused=False))


async def handle_help(message: Message, daily: DailyServices) -> None:
    """``/help``."""
    subscription = await _contact(message, daily)
    if subscription is not None:
        paused = subscription.status is DailySubscriptionStatus.PAUSED
        await message.answer(texts.HELP, reply_markup=menu_keyboard(paused=paused))


async def handle_anything_else(message: Message, daily: DailyServices) -> None:
    """Any other message: a contact like the rest, then the status."""
    if await _contact(message, daily) is not None:
        await _show_status(message, daily)


# --------------------------------------------------------------------------- #
# The contact                                                                 #
# --------------------------------------------------------------------------- #


async def _contact(message: Message, daily: DailyServices) -> DailySubscription | None:
    """Record the contact; return the subscription when the message may go on.

    ``None`` means the contact was fully answered here — refused, or the
    activation that just sent the first item.
    """
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return None
    result = await daily.subscriptions.first_contact(user.id)
    match result.outcome:
        case ContactOutcome.DISABLED | ContactOutcome.NOT_INVITED:
            await message.answer(texts.DISABLED)
        case ContactOutcome.OWNER:  # the owner router answers first; kept as a backstop
            await message.answer(texts.OWNER_HELP)
        case ContactOutcome.UNKNOWN_BUYER:
            await message.answer(texts.no_access(0))
        case ContactOutcome.NO_ACCESS:
            await message.answer(texts.no_access(result.completed_purchases))
        case ContactOutcome.ACTIVATED:
            await _welcome_and_first_item(message, daily, result)
        case ContactOutcome.ACTIVE | ContactOutcome.PAUSED:
            return result.subscription
    return None


async def _welcome_and_first_item(
    message: Message,
    daily: DailyServices,
    result: ContactResult,
) -> None:
    """The welcome first, then the first item — through the delivery engine."""
    subscription = result.subscription
    if subscription is None or subscription.anchor_minute is None:  # pragma: no cover
        return
    try:
        await message.answer(
            texts.welcome(subscription.anchor_minute),
            reply_markup=menu_keyboard(paused=False),
        )
    except TelegramForbiddenError:
        await daily.subscriptions.blocked(subscription.user_id)
        return
    except TelegramAPIError as error:
        # The item may still go through; the engine decides and records it.
        logger.warning("daily_welcome_not_sent", user_id=subscription.user_id, error=str(error))

    if result.delivery is None:  # pragma: no cover — one activation per buyer
        logger.warning("daily_activation_without_delivery", user_id=subscription.user_id)
        return
    report = await daily.delivery.deliver(result.delivery.id)
    if report.step is DeliveryStep.NO_CONTENT:
        with suppress(TelegramAPIError):
            await message.answer(texts.NO_CONTENT)
    logger.info(
        "daily_first_item",
        user_id=subscription.user_id,
        delivery_id=result.delivery.id,
        step=report.step.value,
    )


async def _show_status(message: Message, daily: DailyServices) -> None:
    user = message.from_user
    if user is None:  # pragma: no cover
        return
    current = await daily.subscriptions.status(user.id)
    if current is None or current.subscription.anchor_minute is None:  # pragma: no cover
        return
    paused = current.subscription.status is DailySubscriptionStatus.PAUSED
    await message.answer(
        texts.status(
            received=current.received,
            available=current.available,
            anchor_minute=current.subscription.anchor_minute,
            paused=paused,
        ),
        reply_markup=menu_keyboard(paused=paused),
    )


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="daily_subscriber")
    private = F.chat.type == "private"
    router.message(CommandStart(), private)(handle_start)
    router.message(Command("status"), private)(handle_status)
    router.message(F.text == texts.STATUS_BUTTON, private)(handle_status)
    router.message(Command("pause"), private)(handle_pause)
    router.message(F.text == texts.PAUSE_BUTTON, private)(handle_pause)
    router.message(Command("resume"), private)(handle_resume)
    router.message(F.text == texts.RESUME_BUTTON, private)(handle_resume)
    router.message(Command("help"), private)(handle_help)
    router.message(private)(handle_anything_else)
    return router


__all__ = ["build_router"]
