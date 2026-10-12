"""The loyalty free pack: "🎁 Take it for free" on a product card.

There is no catalogue, so the free pack is whichever pack the buyer opened by
its deep link: the card shows the button while the milestone-10 reward is
waiting, next to the ordinary payment buttons. Pressing it creates no invoice
and asks no payment provider anything — the shop hands the pack over itself.

Everything here is behind ``LOYALTY_ENABLED``: with the programme off no card
shows the button, and one pressed anyway — an old message, or a hand-crafted
callback — gets a neutral "not available" and grants nothing.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram import Router

from app.bot.keyboards import FreePackCallback
from app.bot.middlewares import BotServices
from app.bot.texts import (
    ALREADY_PURCHASED,
    CARD_UNAVAILABLE,
    FREE_PACK_ALREADY_USED,
    FREE_PACK_DELIVERY_FAILED,
    FREE_PACK_FAILED,
    FREE_PACK_GRANTED,
    FREE_PACK_IN_PROGRESS,
    FREE_PACK_NOT_AVAILABLE,
    FREE_PACK_PENDING_INVOICE,
    FREE_PACK_RESENT,
    SECTION_UNAVAILABLE,
)
from app.core.exceptions import (
    AppError,
    DuplicatePurchaseError,
    FreePackAlreadyUsedError,
    FreePackNotAvailableError,
    FreePackPendingCheckoutError,
    LockBusyError,
    LoyaltyDisabledError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from app.domain.entities import Product

logger = get_logger(__name__)

# What each refusal tells the buyer. Every one is an alert: nothing was handed
# over, and the buyer should know why before pressing again.
_REFUSALS: tuple[tuple[type[AppError], str], ...] = (
    (LoyaltyDisabledError, SECTION_UNAVAILABLE),
    (FreePackNotAvailableError, FREE_PACK_NOT_AVAILABLE),
    (FreePackAlreadyUsedError, FREE_PACK_ALREADY_USED),
    (FreePackPendingCheckoutError, FREE_PACK_PENDING_INVOICE),
    (ProductNotFoundError, CARD_UNAVAILABLE),
    (ProductInactiveError, CARD_UNAVAILABLE),
)


async def handle_free_pack_pressed(
    callback: CallbackQuery,
    callback_data: FreePackCallback,
    shop: BotServices,
) -> None:
    """The buyer pressed "🎁 Take it for free" on a product card.

    The pack is always the sender's own: the callback carries only the product,
    so nobody can claim on anybody else's behalf.
    """
    if not shop.loyalty.enabled:
        await callback.answer(SECTION_UNAVAILABLE, show_alert=False)
        return
    try:
        product = await shop.purchases.product_for_checkout(callback_data.product_id)
    except (ProductNotFoundError, ProductInactiveError):
        await callback.answer(CARD_UNAVAILABLE, show_alert=True)
        return
    await _claim(callback, shop, product)


async def _claim(callback: CallbackQuery, shop: BotServices, product: Product) -> None:
    user_id = callback.from_user.id
    try:
        claim = await shop.checkout.claim_free_pack(user_id=user_id, product=product)
    except LockBusyError:
        # A claim or a checkout of this product is running; it will finish.
        await callback.answer(FREE_PACK_IN_PROGRESS, show_alert=False)
        return
    except DuplicatePurchaseError:
        # Bought already: no free pack spent on it, just the link again.
        await callback.answer(ALREADY_PURCHASED, show_alert=True)
        owned = await shop.purchases.find_owned(user_id, product.id)
        if owned is not None:
            with suppress(LockBusyError):
                await shop.checkout.redeliver(owned.id)
        return
    except AppError as error:
        await callback.answer(_refusal(error, user_id=user_id), show_alert=True)
        return

    await callback.answer(FREE_PACK_RESENT if claim.repeated else FREE_PACK_GRANTED)
    if not claim.delivery.succeeded and callback.bot is not None:
        await callback.bot.send_message(chat_id=user_id, text=FREE_PACK_DELIVERY_FAILED)


def _refusal(error: AppError, *, user_id: int) -> str:
    """The buyer-facing reason a claim was refused."""
    for refused, text in _REFUSALS:
        if isinstance(error, refused):
            return text
    logger.error(
        "free_pack_claim_failed",
        telegram_id=user_id,
        error=str(error),
    )
    return FREE_PACK_FAILED


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="free_pack")
    router.callback_query(FreePackCallback.filter())(handle_free_pack_pressed)
    return router


__all__ = ["build_router", "handle_free_pack_pressed"]
