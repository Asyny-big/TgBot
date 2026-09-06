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

from app.bot.handlers.bonuses import show_invitation
from app.bot.keyboards import bonus_hint_keyboard, payment_keyboard
from app.bot.middlewares import BotServices
from app.bot.profile import profile_of
from app.bot.texts import (
    ALREADY_PURCHASED,
    CARD_NOT_FOUND,
    CARD_UNAVAILABLE,
    DELIVERY_FAILED,
    NO_DEEP_LINK,
    product_card,
)
from app.core.exceptions import (
    AppError,
    LockBusyError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.core.logging import get_logger
from app.domain.referrals import ReferralOutcome, is_referral_payload
from app.domain.slug import is_valid_slug

if TYPE_CHECKING:
    from aiogram.types import Message, User

logger = get_logger(__name__)


async def handle_start_without_deep_link(message: Message, shop: BotServices) -> None:
    """No payload: there is nothing to show, and nothing is recorded.

    Still not a catalogue. The bonus section is offered as a button because it
    is the one screen a user may legitimately want without a product link — it
    lists no products and no channels.
    """
    keyboard = bonus_hint_keyboard() if shop.referrals.enabled else None
    await message.answer(NO_DEEP_LINK, reply_markup=keyboard)


async def _handle_invitation(
    message: Message,
    shop: BotServices,
    payload: str,
    user: User,
) -> bool:
    """Try to accept an invitation. ``False`` means "not an invitation after all".

    A referral failure must never break the shop, so anything unexpected is
    logged and treated as "not an invitation": the payload then goes down the
    ordinary product path, which is the behaviour the bot had before.
    """
    try:
        registration = await shop.referrals.register(
            payload=payload,
            profile=profile_of(user),
        )
    except AppError as error:
        logger.error(  # noqa: TRY400 — referral is optional, the shop is not
            "referral_registration_failed",
            telegram_id=user.id,
            error=str(error),
        )
        return False

    if registration.outcome is ReferralOutcome.UNKNOWN_CODE:
        return False

    await show_invitation(message, shop, registration)
    return True


async def handle_deep_link(message: Message, command: CommandObject, shop: BotServices) -> None:
    """Show the product card, or re-send the link when it was already bought."""
    payload = (command.args or "").strip()
    user = message.from_user
    if user is None:  # pragma: no cover — private chats always carry a sender
        return

    if is_referral_payload(payload) and await _handle_invitation(message, shop, payload, user):
        # An invitation records a relationship and shows where to look next; it
        # never opens a product. When the code is unknown we fall through to the
        # ordinary lookup below, so a product whose slug happens to start with
        # "ref_" keeps working exactly as it did.
        return

    if not is_valid_slug(payload):
        await message.answer(CARD_NOT_FOUND)
        return

    try:
        card = await shop.purchases.open_card(profile_of(user), payload)
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
