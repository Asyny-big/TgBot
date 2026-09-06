"""Bonus arithmetic and the checkout pricing port.

Two things live here, and both are deliberately free of infrastructure:

* the **money maths** — pure functions, so the rules that decide what a buyer is
  charged can be read, reviewed and tested without a database;
* the **ports** through which the purchase and delivery services reach the
  referral feature, so those services keep working unchanged when the ports are
  not wired in.

**One bonus is one Telegram Star.** That is not a configured rate, it is the
model: the balance is a count of Stars' worth of discount, and it is spendable
only on a Stars purchase. ``PriceQuote.bonus_amount`` is therefore *derived*
from the unit count rather than stored beside it, so the two can never disagree.

A purchase settled in USDT still earns its inviter a reward, and the Stars
equivalent comes from the **product's own two prices** at the moment of the sale
(``Product.stars_per_usdt``). The shop has already declared that equivalence by
pricing the item in both currencies, so no external price feed is involved, and
no fixed rate can silently go stale. The rate actually used is written onto the
ledger entry, which is what keeps an old reward from being recomputed after the
admin repriced the product.

Rounding always favours the shop: a discount is floored and a reward is floored.
The reason is arithmetic safety rather than greed — ``price_stars`` is an
``Integer`` column and the Stars gateway sends ``int(Decimal(amount))``, so if
rounding could go the other way a fraction of a Star would vanish silently
between the quote and the invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Final, Protocol

from app.domain.enums import Currency

if TYPE_CHECKING:
    from uuid import UUID

    from app.core.config import ReferralSettings
    from app.domain.entities import Purchase
    from app.domain.uow import UnitOfWork

_PERCENT: Final = Decimal(100)

_PRICE_STEP: Final[dict[Currency, Decimal]] = {
    Currency.XTR: Decimal(1),
    Currency.USDT: Decimal("0.01"),
}
"""Smallest amount each currency can actually be charged in."""


def price_step(currency: Currency) -> Decimal:
    """Smallest chargeable amount in this currency."""
    return _PRICE_STEP[currency]


def floor_to_step(value: Decimal, currency: Currency) -> Decimal:
    """Round down to something the provider can charge."""
    step = price_step(currency)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusPolicy:
    """The configured percentages, lifted out of settings into the domain.

    Taking a small value object rather than the whole settings tree keeps the
    maths callable from a test with three literals. Note what is *absent*: no
    exchange rate, because one bonus is one Star by definition.
    """

    discount_percent: int = 10
    reward_percent: int = 15
    max_bonus_payment_percent: int = 50

    @classmethod
    def from_settings(cls, settings: ReferralSettings) -> BonusPolicy:
        """Build the policy from the process configuration."""
        return cls(
            discount_percent=settings.discount_percent,
            reward_percent=settings.reward_percent,
            max_bonus_payment_percent=settings.max_bonus_payment_percent,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceQuote:
    """What the buyer will actually be charged, and why.

    Kept as an explicit breakdown rather than a single number because the
    purchase row stores all three: what the product cost, what was taken off,
    and what was billed. A later price change must not rewrite an old sale.
    """

    base_amount: Decimal
    """The product's list price at the moment of checkout."""

    discount_amount: Decimal = Decimal(0)
    """Referral discount, in the purchase currency."""

    bonus_units: int = 0
    """Bonuses spent. Only ever non-zero on a Stars purchase."""

    currency: Currency = Currency.XTR
    referral_id: UUID | None = None
    """The relationship the discount came from, if any."""

    @property
    def bonus_amount(self) -> Decimal:
        """Money value of the bonuses spent.

        Derived, not stored: one bonus is one Star, so the count *is* the
        amount. Making it a property is what makes that invariant impossible to
        violate by constructing an inconsistent quote.
        """
        return Decimal(self.bonus_units)

    @property
    def charged_amount(self) -> Decimal:
        """What the invoice is issued for."""
        return self.base_amount - self.discount_amount - self.bonus_amount

    @property
    def uses_discount(self) -> bool:
        """Whether a referral discount was applied."""
        return self.discount_amount > 0

    @property
    def uses_bonus(self) -> bool:
        """Whether any bonuses were spent."""
        return self.bonus_units > 0

    @property
    def is_plain(self) -> bool:
        """Whether this is an ordinary, unmodified sale."""
        return not self.uses_discount and not self.uses_bonus


def plain_quote(base_amount: Decimal, currency: Currency) -> PriceQuote:
    """The quote of a shop without a referral programme: list price, no changes."""
    return PriceQuote(base_amount=base_amount, currency=currency)


def discount_for(base_amount: Decimal, currency: Currency, policy: BonusPolicy) -> Decimal:
    """Referral discount on a first purchase, rounded down."""
    if policy.discount_percent <= 0:
        return Decimal(0)
    raw = base_amount * Decimal(policy.discount_percent) / _PERCENT
    return floor_to_step(raw, currency)


def stars_equivalent(
    paid_amount: Decimal,
    currency: Currency,
    *,
    stars_per_usdt: Decimal | None,
) -> Decimal | None:
    """What a settled payment is worth in Telegram Stars.

    A Stars payment is already in Stars. A USDT payment needs the rate the
    product itself declared; without one there is nothing to convert with, and
    ``None`` says so rather than guessing.
    """
    if currency is Currency.XTR:
        return paid_amount
    if stars_per_usdt is None or stars_per_usdt <= 0:
        return None
    return paid_amount * stars_per_usdt


def reward_bonuses(
    paid_amount: Decimal,
    currency: Currency,
    *,
    policy: BonusPolicy,
    stars_per_usdt: Decimal | None = None,
) -> int:
    """Bonuses earned by the inviter from one settled purchase.

    The base is what the buyer *actually paid*, never the list price: a buyer
    who paid 900 Stars after a discount earns the inviter 135, not 150.

    Returns ``0`` when a USDT purchase carries no declared rate — the caller
    logs that and credits nothing, which is safer than inventing a rate.
    """
    if policy.reward_percent <= 0:
        return 0
    stars = stars_equivalent(paid_amount, currency, stars_per_usdt=stars_per_usdt)
    if stars is None or stars <= 0:
        return 0
    earned = stars * Decimal(policy.reward_percent) / _PERCENT
    return int(earned.to_integral_value(rounding=ROUND_FLOOR))


def max_bonus_payment(base_amount: Decimal, policy: BonusPolicy) -> int:
    """Largest number of bonuses allowed against this Stars price.

    Read straight off the price because one bonus is one Star: 1000 ⭐ at 50%
    accepts 500 bonuses, 600 ⭐ accepts 300, 200 ⭐ accepts 100.
    """
    if policy.max_bonus_payment_percent <= 0:
        return 0
    raw = base_amount * Decimal(policy.max_bonus_payment_percent) / _PERCENT
    return int(raw.to_integral_value(rounding=ROUND_FLOOR))


def spendable_bonuses(
    base_amount: Decimal,
    currency: Currency,
    *,
    balance: int,
    policy: BonusPolicy,
) -> int:
    """Bonuses the buyer may spend on this price.

    Bounded by three things at once: the balance, the configured share of the
    price, and the requirement that something is still left to charge — the
    purchases table refuses a zero amount, and an invoice for nothing is not a
    sale.

    Always ``0`` outside Telegram Stars: bonuses are a Stars discount, and
    spending them on a crypto invoice is not a thing the shop offers.
    """
    if currency is not Currency.XTR or balance <= 0:
        return 0

    payable = min(balance, max_bonus_payment(base_amount, policy))
    # Leave at least one Star to actually charge.
    payable = min(payable, int(base_amount) - 1)
    return max(payable, 0)


def quote(  # noqa: PLR0913 — pricing genuinely depends on this many inputs
    base_amount: Decimal,
    currency: Currency,
    *,
    policy: BonusPolicy,
    referral_id: UUID | None = None,
    discount_eligible: bool = False,
    balance: int = 0,
    use_bonus: bool = False,
) -> PriceQuote:
    """Price one checkout.

    The referral discount and bonus spending are mutually exclusive by design:
    the discount exists only on a buyer's first qualifying purchase, and on that
    purchase bonuses are not offered at all. That keeps the economics simple and
    removes a whole family of edge cases — after the first purchase the discount
    is gone for good and bonuses apply from then on.
    """
    if discount_eligible:
        discount = discount_for(base_amount, currency, policy)
        if discount > 0:
            return PriceQuote(
                base_amount=base_amount,
                discount_amount=discount,
                currency=currency,
                referral_id=referral_id,
            )

    if use_bonus:
        spent = spendable_bonuses(base_amount, currency, balance=balance, policy=policy)
        if spent > 0:
            return PriceQuote(
                base_amount=base_amount,
                bonus_units=spent,
                currency=currency,
            )

    return plain_quote(base_amount, currency)


class CheckoutPricing(Protocol):
    """How the purchase service asks the referral feature what to charge.

    ``resolve`` and ``hold_for`` take the caller's unit of work: pricing and the
    reservation of bonuses must commit in the *same* transaction as the purchase
    they belong to, otherwise a crash between the two could bill a buyer for an
    amount whose funding was never recorded.
    """

    async def resolve(
        self,
        uow: UnitOfWork,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
    ) -> PriceQuote:
        """Price a checkout and hold whatever it needs to be funded.

        Raises:
            InsufficientBonusBalanceError: the buyer asked to spend bonuses they
                no longer have — the balance moved between the prompt and the
                button press.
            BonusesNotAvailableError: bonuses were requested on a rail that
                cannot spend them.
        """
        ...

    async def preview(
        self,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
    ) -> PriceQuote:
        """Price a checkout without holding anything. Read only.

        Only a prediction: whoever acts on it must have ``resolve`` re-derive
        the number under a lock before a buyer is committed to it.
        """
        ...

    @property
    def discount_percent(self) -> int:
        """Referral discount this shop gives, as a percentage."""
        ...

    async def discount_eligible(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether this buyer's next purchase carries the referral discount.

        Answered from the caller's transaction so the product card can show the
        reduced price without a second round trip.
        """
        ...

    async def hold_for(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        """Record the hold behind a purchase that was just inserted.

        Split from ``resolve`` only because the purchase id does not exist until
        its row does; both run in the same transaction.
        """
        ...

    async def on_payment_confirmed(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """Turn the holds behind a paid purchase into settled facts.

        Releasing a hold has no hook here on purpose: invoices are expired in
        bulk by housekeeping, so the release pass is a sweep rather than a
        per-purchase callback.
        """
        ...


class SaleCompletion(Protocol):
    """How the delivery service reports a completed sale to the bonus feature.

    Both methods are best effort by contract: the buyer already has their link
    by the time either is called, so neither may fail a delivery.
    """

    async def accrue(self, purchase_id: UUID) -> None:
        """Credit the buyer's inviter, if there is one. Idempotent."""
        ...

    async def invite_buyer(self, user_id: int) -> None:
        """Offer the buyer the referral programme. Never required of them."""
        ...


__all__ = [
    "BonusPolicy",
    "CheckoutPricing",
    "PriceQuote",
    "SaleCompletion",
    "discount_for",
    "floor_to_step",
    "max_bonus_payment",
    "plain_quote",
    "price_step",
    "quote",
    "reward_bonuses",
    "spendable_bonuses",
    "stars_equivalent",
]
