"""Payment handlers: button presses, Stars checkout and refunds.

This is where — and only where — invoices come into existence: every code path
below starts with the buyer pressing ⭐ or 💎.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import PayCallback, crypto_pay_keyboard
from app.bot.middlewares import BotServices
from app.bot.texts import (
    ALREADY_PURCHASED,
    CARD_UNAVAILABLE,
    CRYPTO_INVOICE_CREATED,
    DELIVERY_FAILED,
    PAYMENT_IN_PROGRESS,
    PAYMENT_UNAVAILABLE,
    PRE_CHECKOUT_ALREADY_OWNED,
    PRE_CHECKOUT_UNAVAILABLE,
    PRE_CHECKOUT_UNKNOWN,
    REFUND_NOTICE,
)
from app.core.exceptions import (
    DuplicatePurchaseError,
    LockBusyError,
    PaymentGatewayError,
    ProductInactiveError,
    ProductNotFoundError,
    ProviderNotSupportedError,
    PurchaseNotFoundError,
)
from app.core.logging import get_logger
from app.domain.enums import PaymentProvider, PurchaseStatus

if TYPE_CHECKING:
    from aiogram.types import PreCheckoutQuery

logger = get_logger(__name__)


async def handle_pay_pressed(
    callback: CallbackQuery,
    callback_data: PayCallback,
    shop: BotServices,
) -> None:
    """The buyer pressed a payment button: create the purchase and the invoice."""
    user = callback.from_user
    try:
        product = await shop.purchases.product_for_checkout(callback_data.product_id)
    except (ProductNotFoundError, ProductInactiveError):
        await callback.answer(CARD_UNAVAILABLE, show_alert=True)
        return

    try:
        if callback_data.provider is PaymentProvider.STARS:
            await shop.checkout.start_stars_checkout(user_id=user.id, product=product)
            await callback.answer()
        else:
            checkout = await shop.checkout.start_crypto_checkout(
                user_id=user.id,
                product=product,
            )
            await callback.answer()
            if callback.bot is not None:
                await callback.bot.send_message(
                    chat_id=user.id,
                    text=CRYPTO_INVOICE_CREATED,
                    reply_markup=crypto_pay_keyboard(checkout.pay_url),
                )
    except LockBusyError:
        await callback.answer(PAYMENT_IN_PROGRESS, show_alert=False)
    except DuplicatePurchaseError:
        # Already paid for: no new invoice, just hand the link over again.
        await callback.answer(ALREADY_PURCHASED, show_alert=True)
        owned = await shop.purchases.find_owned(user.id, product.id)
        if owned is not None:
            with suppress(LockBusyError):
                # A delivery for this purchase is already running; it will arrive.
                await shop.checkout.redeliver(owned.id)
    except ProviderNotSupportedError:
        await callback.answer(CARD_UNAVAILABLE, show_alert=True)
    except PaymentGatewayError:
        await callback.answer(PAYMENT_UNAVAILABLE, show_alert=True)


async def handle_pre_checkout(query: PreCheckoutQuery, shop: BotServices) -> None:
    """Telegram's last check before charging: answer within seconds or the payment fails."""
    purchase = await shop.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=query.invoice_payload,
    )
    if purchase is None:
        await query.answer(ok=False, error_message=PRE_CHECKOUT_UNKNOWN)
        return
    if purchase.status in (PurchaseStatus.PAID, PurchaseStatus.DELIVERED):
        await query.answer(ok=False, error_message=PRE_CHECKOUT_ALREADY_OWNED)
        return

    try:
        await shop.purchases.product_for_checkout(purchase.product_id)
    except (ProductNotFoundError, ProductInactiveError):
        await query.answer(ok=False, error_message=PRE_CHECKOUT_UNAVAILABLE)
        return

    if await shop.purchases.find_owned(purchase.user_id, purchase.product_id) is not None:
        await query.answer(ok=False, error_message=PRE_CHECKOUT_ALREADY_OWNED)
        return

    await query.answer(ok=True)


async def handle_successful_payment(message: Message, shop: BotServices) -> None:
    """Stars payment captured: confirm it, then deliver."""
    payment = message.successful_payment
    if payment is None:  # pragma: no cover — guarded by the filter
        return

    try:
        result = await shop.checkout.settle_payment(
            provider=PaymentProvider.STARS,
            external_id=payment.invoice_payload,
            telegram_charge_id=payment.telegram_payment_charge_id,
        )
    except LockBusyError:
        logger.info("stars_settlement_already_running", payload=payment.invoice_payload)
        return
    except PurchaseNotFoundError:
        logger.error(  # noqa: TRY400 — money arrived for an unknown invoice: alert, do not crash
            "stars_payment_without_purchase",
            payload=payment.invoice_payload,
            charge_id=payment.telegram_payment_charge_id,
        )
        await message.answer(DELIVERY_FAILED)
        return

    if not result.succeeded:
        await message.answer(DELIVERY_FAILED)


async def handle_refunded_payment(message: Message, shop: BotServices) -> None:
    """Stars refund: revoke access so the product can be sold again."""
    refund = message.refunded_payment
    if refund is None:  # pragma: no cover — guarded by the filter
        return

    try:
        await shop.purchases.refund_by_charge_id(refund.telegram_payment_charge_id)
    except PurchaseNotFoundError:
        logger.warning(
            "refund_for_unknown_charge",
            charge_id=refund.telegram_payment_charge_id,
        )
        return
    await message.answer(REFUND_NOTICE)


def build_router() -> Router:
    """A fresh router; aiogram allows one parent dispatcher per router instance."""
    router = Router(name="payments")
    router.callback_query(PayCallback.filter())(handle_pay_pressed)
    router.pre_checkout_query()(handle_pre_checkout)
    router.message(F.successful_payment)(handle_successful_payment)
    router.message(F.refunded_payment)(handle_refunded_payment)
    return router
