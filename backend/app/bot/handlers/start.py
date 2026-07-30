"""``/start`` handler: the only entry point into the shop.

A deep link payload is required. There is no catalog, no menu and no product
list — an empty or unknown payload is answered with a short message.

Opening a card creates **no purchase and no invoice**: it reads the product,
remembers the visitor's Telegram profile, and shows payment buttons. Nothing is
billed until one of those buttons is pressed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart

from app.bot.keyboards import payment_keyboard
from app.bot.middlewares import BotServices
from app.bot.texts import (
    ALREADY_PURCHASED,
    CARD_NOT_FOUND,
    CARD_UNAVAILABLE,
    DELIVERY_FAILED,
    NO_DEEP_LINK,
    product_card,
)
from app.core.exceptions import LockBusyError, ProductInactiveError, ProductNotFoundError
from app.core.logging import get_logger
from app.domain.commands import UserDraft
from app.domain.slug import is_valid_slug

if TYPE_CHECKING:
    from aiogram.types import Message, User

logger = get_logger(__name__)


def _profile(user: User) -> UserDraft:
    return UserDraft(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )


async def handle_start_without_deep_link(message: Message) -> None:
    """No payload: there is nothing to show, and nothing is recorded."""
    await message.answer(NO_DEEP_LINK)


async def handle_deep_link(message: Message, command: CommandObject, shop: BotServices) -> None:
    """Show the product card, or re-send the link when it was already bought."""
    payload = (command.args or "").strip()
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return

    if not is_valid_slug(payload):
        await message.answer(CARD_NOT_FOUND)
        return

    try:
        card = await shop.purchases.open_card(_profile(user), payload)
    except ProductNotFoundError:
        await message.answer(CARD_NOT_FOUND)
        return
    except ProductInactiveError:
        await message.answer(CARD_UNAVAILABLE)
        return

    if card.owned_purchase is not None:
        await message.answer(ALREADY_PURCHASED)
        try:
            result = await shop.checkout.redeliver(card.owned_purchase.id)
        except LockBusyError:
            # A delivery for this purchase is already running; it will arrive.
            logger.info("redelivery_already_running", purchase_id=str(card.owned_purchase.id))
            return
        if not result.succeeded:
            await message.answer(DELIVERY_FAILED)
        return

    caption = product_card(card)
    keyboard = payment_keyboard(card)
    if card.product.photo_file_id:
        await message.answer_photo(
            photo=card.product.photo_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
        return
    await message.answer(caption, reply_markup=keyboard, disable_web_page_preview=True)


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="start")
    private = F.chat.type == "private"
    router.message(CommandStart(deep_link=False), private)(handle_start_without_deep_link)
    router.message(CommandStart(deep_link=True), private)(handle_deep_link)
    return router
