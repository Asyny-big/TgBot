"""Domain entities.

Immutable, infrastructure-free representations of the business objects. The
persistence layer maps rows onto these; services and delivery layers never see
SQLAlchemy models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.enums import Currency, PaymentProvider, PurchaseStatus

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class Product:
    """A single digital good, reachable only through its deep link."""

    id: UUID
    slug: str
    title: str
    description: str
    photo_file_id: str | None
    delivery_url: str
    price_stars: int | None
    price_usdt: Decimal | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def price_for(self, provider: PaymentProvider) -> int | Decimal | None:
        """Price in the provider's own currency, or ``None`` when unsupported."""
        if provider is PaymentProvider.STARS:
            return self.price_stars
        return self.price_usdt

    def supports(self, provider: PaymentProvider) -> bool:
        """Whether the product can be bought through this provider."""
        return self.price_for(provider) is not None

    @property
    def available_providers(self) -> tuple[PaymentProvider, ...]:
        """Providers with a configured price, in display order."""
        return tuple(provider for provider in PaymentProvider if self.supports(provider))


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    """A Telegram user who opened at least one product card."""

    telegram_id: int
    username: str | None
    first_name: str | None
    language_code: str | None
    created_at: datetime
    last_seen_at: datetime

    @property
    def display_name(self) -> str:
        """Human readable label for the admin panel."""
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return str(self.telegram_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class Purchase:
    """One attempt to buy one product through one provider."""

    id: UUID
    user_id: int
    product_id: UUID
    provider: PaymentProvider
    status: PurchaseStatus
    amount: Decimal
    currency: Currency
    external_id: str
    telegram_charge_id: str | None
    delivered_url: str | None
    created_at: datetime
    paid_at: datetime | None
    delivered_at: datetime | None

    @property
    def grants_access(self) -> bool:
        """Whether this purchase entitles the buyer to the delivery link."""
        return self.status.grants_access


@dataclass(frozen=True, slots=True, kw_only=True)
class PurchaseRecord:
    """A purchase joined with its buyer and product, for admin listings."""

    purchase: Purchase
    user: User
    product: Product
