"""Write-side input objects consumed by the repositories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import InvalidPriceError
from app.domain.patch import UNSET, Maybe, is_set

if TYPE_CHECKING:
    from decimal import Decimal
    from uuid import UUID

    from app.domain.enums import Currency, PaymentProvider, PurchaseStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductDraft:
    """Everything needed to create a product."""

    slug: str
    title: str
    description: str
    delivery_url: str
    photo_file_id: str | None = None
    price_stars: int | None = None
    price_usdt: Decimal | None = None
    is_active: bool = True

    def __post_init__(self) -> None:
        if self.price_stars is None and self.price_usdt is None:
            raise InvalidPriceError


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductUpdate:
    """Partial product update; omitted fields keep their stored value."""

    slug: Maybe[str] = UNSET
    title: Maybe[str] = UNSET
    description: Maybe[str] = UNSET
    delivery_url: Maybe[str] = UNSET
    photo_file_id: Maybe[str | None] = UNSET
    price_stars: Maybe[int | None] = UNSET
    price_usdt: Maybe[Decimal | None] = UNSET
    is_active: Maybe[bool] = UNSET

    def changes(self) -> dict[str, object]:
        """Return only the supplied fields, ready for an UPDATE statement."""
        candidates: dict[str, Maybe[object]] = {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "delivery_url": self.delivery_url,
            "photo_file_id": self.photo_file_id,
            "price_stars": self.price_stars,
            "price_usdt": self.price_usdt,
            "is_active": self.is_active,
        }
        return {name: value for name, value in candidates.items() if is_set(value)}

    @property
    def is_empty(self) -> bool:
        """Whether the update would not touch any column."""
        return not self.changes()


@dataclass(frozen=True, slots=True, kw_only=True)
class UserDraft:
    """Telegram profile snapshot taken on every interaction."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PurchaseDraft:
    """A purchase in the ``pending`` state, created together with the invoice."""

    user_id: int
    product_id: UUID
    provider: PaymentProvider
    amount: Decimal
    currency: Currency
    external_id: str
    status: PurchaseStatus | None = None
