"""Inline keyboards.

Callback data carries the provider and the product id, so a button press is
self-contained: no server side state between showing the card and paying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode
from uuid import UUID  # noqa: TC003 — CallbackData resolves annotations at runtime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import (
    BONUS_SECTION_BUTTON,
    BONUS_SHARE_BUTTON,
    BONUS_SKIP_BUTTON,
    BONUS_USE_BUTTON,
    CRYPTO_BUTTON,
    CRYPTO_PAY_BUTTON,
    PREVIEW_BUTTON,
    STARS_BUTTON,
)
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


class BonusCallback(CallbackData, prefix="bonus"):
    """Opens the "My bonuses" section."""


class BonusChoiceCallback(CallbackData, prefix="bonususe"):
    """The answer to "spend your bonuses on this purchase?".

    The provider and product travel in the callback data, exactly like
    ``PayCallback``, so the decision stays self-contained: no server side state
    is kept between showing the price and issuing the invoice.
    """

    provider: PaymentProvider
    product_id: UUID
    use_bonus: bool


def preview_keyboard(preview_url: str | None) -> InlineKeyboardMarkup:
    """Where an invited visitor goes next.

    The bot knows exactly one preview URL, from configuration. It stores no
    channel list, so changing which channels exist never touches the bot. When
    the URL is unset the button is simply absent.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if preview_url:
        rows.append([InlineKeyboardButton(text=PREVIEW_BUTTON, url=preview_url)])
    rows.append(
        [InlineKeyboardButton(text=BONUS_SECTION_BUTTON, callback_data=BonusCallback().pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonus_section_keyboard(share_url: str) -> InlineKeyboardMarkup:
    """One button: hand the invitation link to Telegram's own share sheet."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=BONUS_SHARE_BUTTON, url=share_url)]]
    )


def bonus_choice_keyboard(
    *,
    provider: PaymentProvider,
    product_id: UUID,
) -> InlineKeyboardMarkup:
    """Spend bonuses on this purchase, or pay the full price."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BONUS_USE_BUTTON,
                    callback_data=BonusChoiceCallback(
                        provider=provider,
                        product_id=product_id,
                        use_bonus=True,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=BONUS_SKIP_BUTTON,
                    callback_data=BonusChoiceCallback(
                        provider=provider,
                        product_id=product_id,
                        use_bonus=False,
                    ).pack(),
                )
            ],
        ]
    )


def bonus_hint_keyboard() -> InlineKeyboardMarkup:
    """Offer the bonus section after a completed purchase. Never required."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BONUS_SECTION_BUTTON, callback_data=BonusCallback().pack())]
        ]
    )


def share_url(*, referral_link: str, text: str) -> str:
    """Telegram's native share sheet, pre-filled with the invitation.

    Using ``t.me/share`` rather than a bot-side "forward this" flow keeps the
    interaction to a single tap and lets the user pick the recipient in their
    own client.
    """
    query = urlencode({"url": referral_link, "text": text})
    return f"https://t.me/share/url?{query}"
