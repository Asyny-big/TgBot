"""Bonus balance arithmetic and the checkout pricing port.

Two things live here, and both are deliberately free of infrastructure:

* the **money maths** — a set of pure functions, so the rules that decide what a
  buyer is charged can be read, reviewed and tested without a database;
* the **ports** through which the purchase and delivery services reach the
  referral feature, so those services keep working unchanged when the ports are
  not wired in.

Rounding always favours the shop. A discount is floored, a reward is floored,
and the units needed to cover a discount are ceilinged. The reason is not greed
but arithmetic safety: Telegram Stars are integers, ``price_stars`` is an
``Integer`` column and the Stars gateway sends ``int(Decimal(amount))``. If
rounding could ever go the other way, a fraction of a star would vanish
silently between the quote and the invoice.

The bonus balance is a single pool of integer **units**, where one unit is worth
one Telegram Star. Purchases settled in USDT join the same pool through a
configured rate, and the rate in force is recorded on the ledger entry it
produced, so changing the rate later never rewrites history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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


def ceil_to_step(value: Decimal, currency: Currency) -> Decimal:
    """Round up to something the provider can charge."""
    step = price_step(currency)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusPolicy:
    """The configured numbers, lifted out of settings into the domain.

    Taking a small value object rather than the whole settings tree keeps the
    maths callable from a test with three literals.
    """

    discount_percent: int = 10
    reward_percent: int = 15
    max_bonus_payment_percent: int = 50
    units_per_usdt: Decimal = Decimal(500)

    @classmethod
    def from_settings(cls, settings: ReferralSettings) -> BonusPolicy:
        """Build the policy from the process configuration."""
        return cls(
            discount_percent=settings.discount_percent,
            reward_percent=settings.reward_percent,
            max_bonus_payment_percent=settings.max_bonus_payment_percent,
            units_per_usdt=settings.bonus_units_per_usdt,
        )

    def rate_for(self, currency: Currency) -> Decimal | None:
        """Conversion rate recorded on a ledger entry, if one was needed."""
        return None if currency is Currency.XTR else self.units_per_usdt


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

    bonus_amount: Decimal = Decimal(0)
    """Value covered by bonuses, in the purchase currency."""

    bonus_units: int = 0
    """Units debited from the balance to fund ``bonus_amount``."""

    currency: Currency = Currency.XTR
    referral_id: UUID | None = None
    """The relationship the discount came from, if any."""

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
        """Whether any bonus units were spent."""
        return self.bonus_units > 0

    @property
    def is_plain(self) -> bool:
        """Whether this is an ordinary, unmodified sale."""
        return not self.uses_discount and not self.uses_bonus


def plain_quote(base_amount: Decimal, currency: Currency) -> PriceQuote:
    """The quote of a shop without a referral programme: list price, no changes."""
    return PriceQuote(base_amount=base_amount, currency=currency)


def units_for_money(amount: Decimal, currency: Currency, policy: BonusPolicy) -> int:
    """Value of an amount expressed in bonus units, rounded down."""
    if currency is Currency.XTR:
        return int(amount.to_integral_value(rounding=ROUND_FLOOR))
    return int((amount * policy.units_per_usdt).to_integral_value(rounding=ROUND_FLOOR))


def money_for_units(units: int, currency: Currency, policy: BonusPolicy) -> Decimal:
    """Largest amount these units can pay for, rounded down to a chargeable step."""
    if units <= 0:
        return Decimal(0)
    if currency is Currency.XTR:
        return Decimal(units)
    return floor_to_step(Decimal(units) / policy.units_per_usdt, currency)


def units_to_cover(amount: Decimal, currency: Currency, policy: BonusPolicy) -> int:
    """Units required to fund this amount, rounded up.

    Rounded up on purpose: the buyer must never receive a fraction of a step for
    free because the rate did not divide evenly.
    """
    if amount <= 0:
        return 0
    if currency is Currency.XTR:
        return int(amount.to_integral_value(rounding=ROUND_CEILING))
    return int((amount * policy.units_per_usdt).to_integral_value(rounding=ROUND_CEILING))


def discount_for(base_amount: Decimal, currency: Currency, policy: BonusPolicy) -> Decimal:
    """Referral discount on a first purchase, rounded down."""
    if policy.discount_percent <= 0:
        return Decimal(0)
    raw = base_amount * Decimal(policy.discount_percent) / _PERCENT
    return floor_to_step(raw, currency)


def max_bonus_payment(base_amount: Decimal, currency: Currency, policy: BonusPolicy) -> Decimal:
    """Largest share of this price that bonuses are allowed to cover."""
    if policy.max_bonus_payment_percent <= 0:
        return Decimal(0)
    raw = base_amount * Decimal(policy.max_bonus_payment_percent) / _PERCENT
    return floor_to_step(raw, currency)


def reward_units(paid_amount: Decimal, currency: Currency, policy: BonusPolicy) -> int:
    """Bonus units earned by the inviter from one settled purchase.

    The base is what the buyer *actually paid*, never the list price: a buyer who
    paid 900 after a discount earns the inviter 135, not 150.
    """
    if policy.reward_percent <= 0:
        return 0
    value = units_for_money(paid_amount, currency, policy)
    return int(
        (Decimal(value) * Decimal(policy.reward_percent) / _PERCENT).to_integral_value(
            rounding=ROUND_FLOOR,
        )
    )


def spendable_units(
    base_amount: Decimal,
    currency: Currency,
    *,
    balance: int,
    policy: BonusPolicy,
) -> tuple[int, Decimal]:
    """Units the buyer may spend on this price, and what they are worth.

    Bounded by three things at once: the balance, the configured share of the
    price, and what the units convert to at a chargeable step. Returns
    ``(0, 0)`` when nothing can usefully be spent.
    """
    if balance <= 0:
        return 0, Decimal(0)

    allowed = max_bonus_payment(base_amount, currency, policy)
    affordable = money_for_units(balance, currency, policy)
    payable = min(allowed, affordable)
    if payable <= 0:
        return 0, Decimal(0)

    # Never leave nothing to charge: the purchases table rejects a zero amount,
    # and an invoice for nothing is not a sale.
    payable = min(payable, base_amount - price_step(currency))
    if payable <= 0:
        return 0, Decimal(0)

    units = min(units_to_cover(payable, currency, policy), balance)
    worth = min(payable, money_for_units(units, currency, policy))
    if units <= 0 or worth <= 0:
        return 0, Decimal(0)
    return units, worth


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
        units, worth = spendable_units(base_amount, currency, balance=balance, policy=policy)
        if units > 0:
            return PriceQuote(
                base_amount=base_amount,
                bonus_amount=worth,
                bonus_units=units,
                currency=currency,
            )

    return plain_quote(base_amount, currency)


class CheckoutPricing(Protocol):
    """How the purchase service asks the referral feature what to charge.

    Both methods take the caller's unit of work: pricing and the reservation of
    bonus units must commit in the *same* transaction as the purchase they
    belong to, otherwise a crash between the two could bill a buyer for an
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
    "ceil_to_step",
    "discount_for",
    "floor_to_step",
    "max_bonus_payment",
    "money_for_units",
    "plain_quote",
    "price_step",
    "quote",
    "reward_units",
    "spendable_units",
    "units_for_money",
    "units_to_cover",
]
