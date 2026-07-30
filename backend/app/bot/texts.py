"""User facing texts.

Kept in one module so wording can be reviewed without reading handler logic.
HTML parse mode is used, and every value interpolated from the database is
escaped — a product title is admin input, not trusted markup.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from app.domain.cards import ProductCard

STARS_BUTTON: Final = "⭐ Telegram Stars"
CRYPTO_BUTTON: Final = "💎 CryptoBot (USDT)"

CARD_NOT_FOUND: Final = "Товар не найден. Проверьте ссылку — возможно, она устарела."
CARD_UNAVAILABLE: Final = "Этот товар сейчас недоступен."
NO_DEEP_LINK: Final = (
    "Этот бот открывается только по прямой ссылке на товар.\n"
    "Попросите продавца прислать вам ссылку ещё раз."
)
ALREADY_PURCHASED: Final = "Вы уже приобретали этот товар. Отправляю ссылку повторно."
PAYMENT_IN_PROGRESS: Final = "Оплата уже обрабатывается. Подождите пару секунд."
TOO_FAST: Final = "Слишком много запросов. Подождите пару секунд."
PAYMENT_UNAVAILABLE: Final = "Не удалось создать счёт на оплату. Попробуйте ещё раз через минуту."
DELIVERY_FAILED: Final = (
    "Оплата получена, но отправить ссылку не удалось. "
    "Откройте ссылку на товар снова — бот выдаст доступ."
)
CRYPTO_INVOICE_CREATED: Final = (
    "Счёт создан. Оплатите его в CryptoBot — доступ придёт сюда автоматически."
)
CRYPTO_PAY_BUTTON: Final = "💎 Оплатить в CryptoBot"
PRE_CHECKOUT_UNAVAILABLE: Final = "Товар больше недоступен, оплата отменена."
PRE_CHECKOUT_ALREADY_OWNED: Final = "Этот товар уже куплен — оплата не нужна."
PRE_CHECKOUT_UNKNOWN: Final = "Счёт устарел. Откройте ссылку на товар заново."
REFUND_NOTICE: Final = "Возврат оформлен. Доступ к товару отозван."


def _money(amount: int | Decimal) -> str:
    """Render an amount without trailing zeros for whole numbers."""
    value = Decimal(amount)
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return f"{value.normalize():f}"


def product_card(card: ProductCard) -> str:
    """Caption of the product card: title, description and prices."""
    lines = [f"<b>{escape(card.product.title)}</b>"]
    if card.product.description:
        lines.append("")
        lines.append(escape(card.product.description))
    lines.append("")
    for option in card.options:
        symbol = "⭐" if option.currency.value == "XTR" else "💎"
        lines.append(f"{symbol} {_money(option.amount)} {option.currency.value}")
    return "\n".join(lines)


def delivery_message(*, product_title: str, delivery_url: str, is_repeat: bool) -> str:
    """The message that actually hands the purchase over."""
    header = "Ваша ссылка на товар:" if is_repeat else "Спасибо за покупку."
    return f"{header}\n<b>{escape(product_title)}</b>\n\n{escape(delivery_url)}"


def invoice_description(description: str) -> str:
    """Invoice description; Telegram requires a non-empty string."""
    return description.strip() or "Цифровой товар"
