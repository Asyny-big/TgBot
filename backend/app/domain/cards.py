"""Product card: what the bot renders after a deep link is opened."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

    from app.domain.entities import Product, Purchase
    from app.domain.enums import Currency, PaymentProvider


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentOption:
    """One payment button on the card."""

    provider: PaymentProvider
    amount: int | Decimal
    """The product's list price. Never changed by the referral feature."""

    currency: Currency

    discounted_amount: Decimal | None = None
    """What this visitor would actually pay, when that is less than the list.

    ``None`` for every ordinary buyer, which is what keeps the card they see
    byte for byte the card it has always been.
    """

    @property
    def is_discounted(self) -> bool:
        """Whether this visitor sees a reduced price."""
        return self.discounted_amount is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductCard:
    """The card plus the buyer's history with this product.

    When ``owned_purchase`` is set the buyer already paid: the caller must hand
    the link over again instead of showing payment buttons.
    """

    product: Product
    options: tuple[PaymentOption, ...] = field(default_factory=tuple)
    owned_purchase: Purchase | None = None

    discount_percent: int = 0
    """Referral discount awaiting this visitor, as a percentage. ``0`` for most."""

    @property
    def is_owned(self) -> bool:
        """Whether this buyer already paid for the product."""
        return self.owned_purchase is not None

    @property
    def is_discounted(self) -> bool:
        """Whether a referral discount is being shown on this card."""
        return self.discount_percent > 0 and any(option.is_discounted for option in self.options)
