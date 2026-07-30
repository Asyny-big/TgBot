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
    currency: Currency


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductCard:
    """The card plus the buyer's history with this product.

    When ``owned_purchase`` is set the buyer already paid: the caller must hand
    the link over again instead of showing payment buttons.
    """

    product: Product
    options: tuple[PaymentOption, ...] = field(default_factory=tuple)
    owned_purchase: Purchase | None = None

    @property
    def is_owned(self) -> bool:
        """Whether this buyer already paid for the product."""
        return self.owned_purchase is not None
