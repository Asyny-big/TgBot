"""Payment handlers: button presses, Stars checkout and refunds.

This is where — and only where — invoices come into existence: every code path
below starts with the buyer pressing ⭐ or 💎.
"""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    BonusChoiceCallback,
    PayCallback,
    bonus_choice_keyboard,
    crypto_pay_keyboard,
)
from app.bot.middlewares import BotServices
from app.bot.texts import (
    ALREADY_PURCHASED,
    BONUS_BALANCE_CHANGED,
    BONUS_STARS_ONLY,
    CARD_UNAVAILABLE,
    CRYPTO_INVOICE_CREATED,
    DELIVERY_FAILED,
    PAYMENT_IN_PROGRESS,
    PAYMENT_UNAVAILABLE,
    PRE_CHECKOUT_ALREADY_OWNED,
    PRE_CHECKOUT_UNAVAILABLE,
    PRE_CHECKOUT_UNKNOWN,
    REFUND_NOTICE,
    bonus_prompt,
)
from app.core.exceptions import (
    AppError,
    BonusesNotAvailableError,
    ConflictError,
    DuplicatePurchaseError,
    InsufficientBonusBalanceError,
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

    from app.domain.entities import Product
    from app.services.bonuses import BonusOffer

logger = get_logger(__name__)


async def handle_pay_pressed(
    callback: CallbackQuery,
    callback_data: PayCallback,
    shop: BotServices,
) -> None:
    """The buyer pressed a payment button.

    One question may come between the button and the invoice: if the buyer has
    bonuses that could reduce *this* price, they are asked first. Everything
    else — and every buyer without bonuses — goes straight to checkout exactly
    as before.
    """
    user = callback.from_user
    try:
        product = await shop.purchases.product_for_checkout(callback_data.product_id)
    except (ProductNotFoundError, ProductInactiveError):
        await callback.answer(CARD_UNAVAILABLE, show_alert=True)
        return

    offer = await _bonus_offer(
        shop, user_id=user.id, product=product, provider=callback_data.provider
    )
    if offer is not None:
        await callback.answer()
        await _ask_about_bonuses(
            callback, product=product, provider=callback_data.provider, offer=offer
        )
        return

    await _start_checkout(
        callback,
        shop,
        product=product,
        provider=callback_data.provider,
        use_bonus=False,
    )


async def handle_bonus_choice(
    callback: CallbackQuery,
    callback_data: BonusChoiceCallback,
    shop: BotServices,
) -> None:
    """The buyer answered "spend your bonuses?" — now issue the invoice."""
    try:
        product = await shop.purchases.product_for_checkout(callback_data.product_id)
    except (ProductNotFoundError, ProductInactiveError):
        await callback.answer(CARD_UNAVAILABLE, show_alert=True)
        return

    await _start_checkout(
        callback,
        shop,
        product=product,
        provider=callback_data.provider,
        use_bonus=callback_data.use_bonus,
    )


async def _bonus_offer(
    shop: BotServices,
    *,
    user_id: int,
    product: Product,
    provider: PaymentProvider,
) -> BonusOffer | None:
    """The bonus question worth asking, or ``None`` to skip straight to paying.

    Never raises: a bonus lookup that fails must not stop a sale. The buyer
    simply is not offered bonuses and pays the ordinary price.
    """
    price = product.price_for(provider)
    if price is None:  # pragma: no cover — the button only exists with a price
        return None
    try:
        offer = await shop.bonuses.offer_for(
            user_id=user_id,
            base_amount=Decimal(price),
            currency=provider.currency,
        )
    except AppError as error:
        logger.error(  # noqa: TRY400 — bonuses are optional, the sale is not
            "bonus_offer_unavailable",
            user_id=user_id,
            product_id=str(product.id),
            error=str(error),
        )
        return None
    return offer if offer.available else None


async def _ask_about_bonuses(
    callback: CallbackQuery,
    *,
    product: Product,
    provider: PaymentProvider,
    offer: BonusOffer,
) -> None:
    """Show the arithmetic before anything is charged or held."""
    priced = offer.quote_with_bonus
    if priced is None or callback.bot is None:  # pragma: no cover — guarded upstream
        return
    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=bonus_prompt(
            balance=offer.balance,
            base_amount=priced.base_amount,
            bonus_amount=priced.bonus_amount,
            charged_amount=priced.charged_amount,
        ),
        reply_markup=bonus_choice_keyboard(provider=provider, product_id=product.id),
    )


async def _start_checkout(
    callback: CallbackQuery,
    shop: BotServices,
    *,
    product: Product,
    provider: PaymentProvider,
    use_bonus: bool,
) -> None:
    """Create the purchase and the invoice. The original payment path."""
    user = callback.from_user
    try:
        if provider is PaymentProvider.STARS:
            await shop.checkout.start_stars_checkout(
                user_id=user.id,
                product=product,
                use_bonus=use_bonus,
            )
            await callback.answer()
        else:
            checkout = await shop.checkout.start_crypto_checkout(
                user_id=user.id,
                product=product,
                use_bonus=use_bonus,
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
    except InsufficientBonusBalanceError:
        # The balance moved between the question and the answer. Refusing is
        # safer than silently charging a price the buyer never agreed to.
        await callback.answer(BONUS_BALANCE_CHANGED, show_alert=True)
    except BonusesNotAvailableError:
        # Bonuses are a Stars discount; the bot never offers them on a crypto
        # card, so this is a hand-crafted callback rather than a real buyer.
        logger.warning(
            "bonus_spend_refused_for_provider",
            user_id=user.id,
            provider=provider.value,
        )
        await callback.answer(BONUS_STARS_ONLY, show_alert=True)
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
    except ConflictError:
        # The price moved while the provider invoice was being created; the
        # orphaned invoice expires unpaid and the buyer simply retries.
        await callback.answer(PAYMENT_UNAVAILABLE, show_alert=True)
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
    router.callback_query(BonusChoiceCallback.filter())(handle_bonus_choice)
    router.pre_checkout_query()(handle_pre_checkout)
    router.message(F.successful_payment)(handle_successful_payment)
    router.message(F.refunded_payment)(handle_refunded_payment)
    return router
