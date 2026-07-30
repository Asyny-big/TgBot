"""Inline keyboards.

Callback data carries the provider and the product id, so a button press is
self-contained: no server side state between showing the card and paying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID  # noqa: TC003 — CallbackData resolves annotations at runtime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import CRYPTO_BUTTON, CRYPTO_PAY_BUTTON, STARS_BUTTON
from app.domain.enums import PaymentProvider

if TYPE_CHECKING:
    from app.domain.cards import ProductCard

_BUTTON_LABELS: Final[dict[PaymentProvider, str]] = {
    PaymentProvider.STARS: STARS_BUTTON,
    PaymentProvider.CRYPTO: CRYPTO_BUTTON,
}


class PayCallback(CallbackData, prefix="pay"):
    """ "Pay with X" button payload."""

    provider: PaymentProvider
    product_id: UUID


def payment_keyboard(card: ProductCard) -> InlineKeyboardMarkup:
    """One button per rail that has a price. Pressing one starts the checkout."""
    rows = [
        [
            InlineKeyboardButton(
                text=_BUTTON_LABELS[option.provider],
                callback_data=PayCallback(
                    provider=option.provider,
                    product_id=card.product.id,
                ).pack(),
            )
        ]
        for option in card.options
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def crypto_pay_keyboard(pay_url: str) -> InlineKeyboardMarkup:
    """Link button that opens the freshly created CryptoBot invoice."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=CRYPTO_PAY_BUTTON, url=pay_url)]]
    )
