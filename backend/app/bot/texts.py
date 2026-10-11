"""User facing texts.

Kept in one module so wording can be reviewed without reading handler logic.
HTML parse mode is used, and every value interpolated from the database is
escaped — a product title is admin input, not trusted markup.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING, Final

from app.domain.loyalty import (
    DISCOUNT_MILESTONE,
    FREE_PACK_MILESTONE,
    VIP_MILESTONE,
    MilestoneRewardState,
)

if TYPE_CHECKING:
    from app.domain.cards import ProductCard
    from app.domain.loyalty import LoyaltyProgress

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
CRYPTO_INVOICE_RESUMED: Final = (
    "У вас уже есть неоплаченный счёт на этот товар — новый не создаётся. "
    "Оплатите его в CryptoBot — доступ придёт сюда автоматически."
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
    """Caption of the product card: title, description and prices.

    An invited buyer with an unused referral discount sees the list price struck
    through beside what they will actually pay. Everybody else sees exactly the
    card this shop has always shown — the discounted line only exists when the
    option carries one.
    """
    lines = [f"<b>{escape(card.product.title)}</b>"]
    if card.product.description:
        lines.append("")
        lines.append(escape(card.product.description))
    lines.append("")
    for option in card.options:
        symbol = "⭐" if option.currency.value == "XTR" else "💎"
        if option.discounted_amount is not None:
            price = f"<s>{_money(option.amount)}</s> {_money(option.discounted_amount)}"
        else:
            price = _money(option.amount)
        lines.append(f"{symbol} {price} {option.currency.value}")
    if card.is_discounted:
        lines.append("")
        lines.append(f"🎁 Скидка {card.discount_percent}% по приглашению")
    if card.is_loyalty_discounted:
        lines.append("")
        lines.append(f"🏆 Скидка {card.loyalty_percent}% по программе лояльности")
    if card.free_pack_available:
        lines.append("")
        lines.append(FREE_PACK_CARD_LINE)
    return "\n".join(lines)


def delivery_message(*, product_title: str, delivery_url: str, is_repeat: bool) -> str:
    """The message that actually hands the purchase over."""
    header = "Ваша ссылка на товар:" if is_repeat else "Спасибо за покупку."
    return f"{header}\n<b>{escape(product_title)}</b>\n\n{escape(delivery_url)}"


def invoice_description(description: str) -> str:
    """Invoice description; Telegram requires a non-empty string."""
    return description.strip() or "Цифровой товар"


# --------------------------------- Referral --------------------------------- #

REFERRAL_WELCOME_HEADER: Final = "🎁 Вас пригласили!"
REFERRAL_ALREADY_LINKED: Final = (
    "Вы уже переходили по приглашению. Ссылка на товар откроет карточку как обычно."
)
REFERRAL_SELF: Final = "Нельзя пригласить самого себя."
REFERRAL_NOT_ELIGIBLE: Final = (
    "Скидка по приглашению доступна только для первой покупки.\n"
    "Вы уже покупали у нас — ссылки на товары работают как обычно."
)
REFERRAL_UNKNOWN: Final = (
    "Приглашение не найдено — возможно, ссылка устарела.\nПопросите прислать её заново."
)

PREVIEW_BUTTON: Final = "📣 Смотреть preview"
PREVIEW_UNAVAILABLE: Final = "Каталог preview пока недоступен. Попросите продавца прислать ссылку."

BONUS_SECTION_BUTTON: Final = "🎁 Мои бонусы"
BONUS_SHARE_BUTTON: Final = "📤 Пригласить друга"
BONUS_USE_BUTTON: Final = "✅ Использовать бонусы"
BONUS_SKIP_BUTTON: Final = "❌ Без бонусов"

BONUS_DISABLED: Final = "Бонусная программа сейчас недоступна."
BONUS_UNAVAILABLE: Final = "Не удалось открыть раздел бонусов. Попробуйте ещё раз через минуту."
BONUS_BALANCE_CHANGED: Final = (
    "Баланс бонусов изменился. Откройте товар заново, чтобы увидеть актуальную цену."
)
BONUS_NOT_FOR_PRODUCT: Final = "Бонусы недоступны для этого товара."
PURCHASE_COMPLETE_HINT: Final = "🎁 Приглашай друзей и получай бонусы с их покупок."


def referral_welcome(*, discount_percent: int, preview_available: bool) -> str:
    """Greeting shown when an invitation link is opened.

    Says what the visitor gets and where to look next — it does not show a
    product, because a referral link is not a product link.
    """
    lines = [
        REFERRAL_WELCOME_HEADER,
        "",
        f"Для вас доступна скидка {discount_percent}% на первую покупку.",
    ]
    if preview_available:
        lines += ["", "👀 Все preview можно посмотреть здесь."]
    else:
        lines += ["", "Ссылку на товар пришлёт продавец."]
    return "\n".join(lines)


def bonus_section(
    *,
    balance: int,
    invited_count: int,
    referral_purchase_count: int,
    referral_link: str,
    reward_percent: int,
) -> str:
    """The "My bonuses" screen.

    Four numbers and a link. Bonuses are shown as a plain balance and never as
    "⭐ Telegram Stars": they are an internal balance, not real Stars, and
    labelling them as Stars would promise something the shop cannot deliver.
    """
    return "\n".join(
        [
            "🎁 <b>Мои бонусы</b>",
            "",
            f"💰 Баланс: {balance}",
            "",
            f"👥 Приглашено: {invited_count}",
            f"🛍 Покупок рефералов: {referral_purchase_count}",
            "",
            f"Получай {reward_percent}% бонусами с каждой покупки",
            "приглашённых тобой пользователей.",
            "",
            "Бонусы можно использовать",
            "при оплате следующих покупок.",
            "",
            "Твоя ссылка:",
            f"<code>{escape(referral_link)}</code>",
        ]
    )


def referral_share_text(*, referral_link: str, discount_percent: int) -> str:
    """Message the buyer forwards to a friend. Deliberately two lines."""
    return f"🎁 Тебе доступна скидка {discount_percent}% на первую покупку:\n{referral_link}"


def pending_invoice_exists(*, amount: int | Decimal, currency: str) -> str:
    """The buyer's existing unpaid invoice differs from what they would pay now."""
    return "\n".join(
        [
            f"У вас уже есть неоплаченный счёт на этот товар: {_money(amount)} {currency}.",
            "Его сумма отличается от текущей цены, поэтому новый счёт не создан.",
            "",
            "Оплатите существующий счёт или дождитесь, пока он истечёт, и попробуйте снова.",
        ]
    )


def bonus_prompt(
    *,
    balance: int,
    base_amount: int | Decimal,
    bonus_amount: int | Decimal,
    charged_amount: int | Decimal,
) -> str:
    """Asks whether to spend bonuses, showing the arithmetic before it happens."""
    return "\n".join(
        [
            f"💰 У тебя есть {balance} бонусов.",
            "Использовать их для этой покупки?",
            "",
            f"Цена: {_money(base_amount)}",
            f"Бонусы: −{_money(bonus_amount)}",
            f"К оплате: {_money(charged_amount)}",
        ]
    )


def reward_notice(
    *,
    units: int,
    balance: int,
    paid_amount: int | Decimal,
) -> str:
    """Tells an inviter that a referral's purchase earned them bonuses."""
    return "\n".join(
        [
            "🎉 Тебе начислены бонусы!",
            "",
            "Твой реферал совершил покупку",
            f"на {_money(paid_amount)}.",
            "",
            f"Твой бонус: +{units}",
            "",
            f"💰 Баланс: {balance}",
        ]
    )


# --------------------------------- Loyalty ---------------------------------- #

PROGRESS_SECTION_BUTTON: Final = "🏆 Мой прогресс"
DAILY_ENTRY_BUTTON: Final = "👑 Открыть VIP Daily"
"""A deep link into the Daily Content bot — offered to VIP buyers only."""
PROGRESS_UNAVAILABLE: Final = "Не удалось открыть прогресс. Попробуйте ещё раз через минуту."
SECTION_UNAVAILABLE: Final = "Этот раздел сейчас недоступен."

FREE_PACK_BUTTON: Final = "🎁 Забрать бесплатно"
FREE_PACK_CARD_LINE: Final = "🎁 Этот пак можно забрать бесплатно по программе лояльности"
FREE_PACK_PROGRESS_HINT: Final = "Откройте ссылку на любой пак и нажмите «🎁 Забрать бесплатно»."
FREE_PACK_GRANTED: Final = "🎁 Бесплатный пак ваш!"
FREE_PACK_RESENT: Final = "Этот пак вы уже забрали. Отправляю ссылку ещё раз."
FREE_PACK_NOT_AVAILABLE: Final = "Бесплатный пак пока недоступен."
FREE_PACK_ALREADY_USED: Final = "Бесплатный пак уже получен."
FREE_PACK_PENDING_INVOICE: Final = (
    "У вас есть неоплаченный счёт на этот пак. "
    "Оплатите его или дождитесь, пока он истечёт, — тогда пак можно забрать бесплатно."
)
FREE_PACK_IN_PROGRESS: Final = "Уже обрабатывается. Подождите пару секунд."
FREE_PACK_FAILED: Final = "Не удалось выдать бесплатный пак. Попробуйте ещё раз через минуту."
FREE_PACK_DELIVERY_FAILED: Final = (
    "Пак ваш, но отправить ссылку не удалось. "
    "Нажмите «🎁 Забрать бесплатно» ещё раз или откройте ссылку на товар — бот выдаст доступ."
)

LOYALTY_MILESTONE_NOTICES: Final[dict[int, str]] = {
    DISCOUNT_MILESTONE: (
        "🎉 Поздравляем! Это ваша 5-я покупка. Вам доступна скидка 50% на следующий пак "
        "— она применится автоматически при оплате."
    ),
    FREE_PACK_MILESTONE: (
        "🎁 10 покупок! Вам доступен бесплатный пак. "
        "Откройте любой пак и нажмите «🎁 Забрать бесплатно»."
    ),
    VIP_MILESTONE: "👑 15 покупок — поздравляем! Вы достигли VIP-статуса.",
}


def loyalty_milestone_notice(milestone: int) -> str:
    """The message a delivery sends when it reaches 5, 10 or 15 purchases.

    Raises:
        KeyError: for anything else — no other rank is ever announced.
    """
    return LOYALTY_MILESTONE_NOTICES[milestone]


_PLURAL_FEW_MIN: Final = 2
_PLURAL_FEW_MAX: Final = 4
_TEENS: Final = range(11, 15)


def _purchases(count: int) -> str:
    """``1 покупка``, ``3 покупки``, ``8 покупок``, ``12 покупок``."""
    if count % 10 == 1 and count % 100 != 11:  # noqa: PLR2004 — Russian plural rule
        word = "покупка"
    elif _PLURAL_FEW_MIN <= count % 10 <= _PLURAL_FEW_MAX and count % 100 not in _TEENS:
        word = "покупки"
    else:
        word = "покупок"
    return f"{count} {word}"


def loyalty_progress(progress: LoyaltyProgress) -> str:
    """The "🏆 My progress" screen.

    Says what the buyer has and what is missing. There is no button to *use* a
    reward here — the 50% discount applies by itself at checkout, and an
    available free pack is claimed from a product card, which the hint says.
    """
    lines = ["🏆 <b>Мой прогресс</b>", "", f"Покупок: {progress.completed_count}", ""]

    match progress.discount_50:
        case MilestoneRewardState.AVAILABLE:
            lines.append("🎉 Скидка 50% доступна")
        case MilestoneRewardState.IN_USE:
            lines.append("⏳ Скидка 50% ждёт оплаты счёта")
        case MilestoneRewardState.USED:
            lines.append("✅ Скидка 50% использована")
        case MilestoneRewardState.LOCKED:
            lines.append(f"До скидки 50%: {_purchases(progress.to_discount)}")

    match progress.free_pack:
        case MilestoneRewardState.AVAILABLE:
            lines.append("🎁 Бесплатный пак доступен")
            lines.append(FREE_PACK_PROGRESS_HINT)
        case MilestoneRewardState.USED:
            lines.append("✅ Бесплатный пак получен")
        case MilestoneRewardState.LOCKED | MilestoneRewardState.IN_USE:
            # A free pack is never "in use"; the database refuses that state.
            lines.append(f"До бесплатного пака: {_purchases(progress.to_free)}")

    if progress.vip:
        lines.append("👑 VIP")
    else:
        lines.append(f"До VIP: {_purchases(progress.to_vip)}")
    return "\n".join(lines)
