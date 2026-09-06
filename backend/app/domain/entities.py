"""Domain entities.

Immutable, infrastructure-free representations of the business objects. The
persistence layer maps rows onto these; services and delivery layers never see
SQLAlchemy models.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from app.domain.enums import BonusTransactionType, Currency, PaymentProvider, PurchaseStatus

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

_NO_MONEY: Final = Decimal(0)
"""Default for a money field that simply did not happen."""


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

    referral_code: str | None = None
    """Permanent, opaque code behind this user's invitation link."""

    bonus_balance: int = 0
    """Spendable bonus units. Not Telegram Stars, not withdrawable, not money."""

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

    base_amount: Decimal | None = None
    """List price at the time of sale. ``None`` on rows predating the feature."""

    discount_amount: Decimal = _NO_MONEY
    """Referral discount taken off the list price."""

    bonus_amount: Decimal = _NO_MONEY
    """Part of the price covered from the buyer's bonus balance."""

    @property
    def grants_access(self) -> bool:
        """Whether this purchase entitles the buyer to the delivery link."""
        return self.status.grants_access

    @property
    def charged_amount(self) -> Decimal:
        """What the buyer was actually billed. Always ``amount``."""
        return self.amount

    @property
    def list_amount(self) -> Decimal:
        """List price of this sale, falling back to the billed amount.

        Purchases created before the referral feature existed carry no
        breakdown, and for those the billed amount *was* the list price.
        """
        return self.base_amount if self.base_amount is not None else self.amount

    @property
    def was_reduced(self) -> bool:
        """Whether anything was taken off the list price for this sale."""
        return self.discount_amount > 0 or self.bonus_amount > 0


@dataclass(frozen=True, slots=True, kw_only=True)
class PurchaseRecord:
    """A purchase joined with its buyer and product, for admin listings."""

    purchase: Purchase
    user: User
    product: Product


@dataclass(frozen=True, slots=True, kw_only=True)
class Referral:
    """One buyer, permanently attributed to the person who invited them.

    The attribution never moves: if A invited B, a later link from C cannot
    take B over. That is a unique constraint in the database, not a convention.
    """

    id: UUID
    referrer_user_id: int
    referred_user_id: int
    created_at: datetime
    discount_used_at: datetime | None = None
    discount_purchase_id: UUID | None = None

    @property
    def discount_available(self) -> bool:
        """Whether the first-purchase discount is still unused."""
        return self.discount_used_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusTransaction:
    """One movement of the internal bonus balance.

    Bonuses are not Telegram Stars and not money: they cannot be withdrawn or
    transferred, and they exist only to reduce a future price in this shop.
    """

    id: UUID
    user_id: int
    amount: int
    """Signed bonus units: positive credits the balance, negative debits it."""

    type: BonusTransactionType
    referral_id: UUID | None = None
    purchase_id: UUID | None = None
    rate_units_per_usdt: Decimal | None = None
    """Rate in force when a USDT amount was converted into units."""

    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusSummary:
    """Everything the "My bonuses" screen shows.

    Deliberately four numbers and a link. The section is a way to invite a
    friend and see the balance, not an analytics dashboard.
    """

    user_id: int
    balance: int
    referral_code: str
    invited_count: int
    referral_purchase_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferralRecord:
    """A referral relationship joined with both parties, for the admin panel."""

    referral: Referral
    referrer: User
    referred: User
    referred_purchase_count: int
    reward_total: int
