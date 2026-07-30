"""Repository contracts.

Structural (``Protocol``) interfaces: the domain declares what it needs, the
infrastructure layer satisfies it without importing anything from here at
runtime. Services depend on these types only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from app.domain.commands import ProductDraft, ProductUpdate, PurchaseDraft, UserDraft
    from app.domain.entities import Product, Purchase, PurchaseRecord, User
    from app.domain.enums import PaymentProvider
    from app.domain.pagination import Page, PageRequest, ProductFilters, PurchaseFilters
    from app.domain.stats import RevenueSummary, StatsPeriod, TopProduct


class ProductRepository(Protocol):
    """Persistence contract for products."""

    async def get(self, product_id: UUID) -> Product | None:
        """Return the product with this id, or ``None``."""
        ...

    async def get_by_slug(self, slug: str, *, only_active: bool = False) -> Product | None:
        """Return the product behind this deep-link slug, or ``None``."""
        ...

    async def list_products(self, filters: ProductFilters, page: PageRequest) -> Page[Product]:
        """Return a filtered, paginated slice of products, newest first."""
        ...

    async def create(self, draft: ProductDraft) -> Product:
        """Insert a product.

        Raises:
            SlugAlreadyExistsError: the slug is taken.
        """
        ...

    async def update(self, product_id: UUID, changes: ProductUpdate) -> Product:
        """Apply a partial update and return the stored product.

        Raises:
            ProductNotFoundError: no product with this id.
            SlugAlreadyExistsError: the new slug is taken.
        """
        ...

    async def delete(self, product_id: UUID) -> None:
        """Delete a product.

        Raises:
            ProductNotFoundError: no product with this id.
            ConflictError: the product has purchases and must be kept.
        """
        ...

    async def count(self, *, only_active: bool | None = None) -> int:
        """Number of products, optionally restricted by activity."""
        ...


class UserRepository(Protocol):
    """Persistence contract for Telegram users."""

    async def get(self, telegram_id: int) -> User | None:
        """Return the user with this Telegram id, or ``None``."""
        ...

    async def upsert(self, draft: UserDraft, *, seen_at: datetime | None = None) -> User:
        """Insert the user or refresh their profile snapshot and last-seen time."""
        ...

    async def count(self) -> int:
        """Total number of known users."""
        ...


class PurchaseRepository(Protocol):
    """Persistence contract for purchases."""

    async def create(self, draft: PurchaseDraft) -> Purchase:
        """Insert a pending purchase.

        Raises:
            DuplicatePurchaseError: the buyer already owns this product, or the
                provider invoice id is already recorded.
        """
        ...

    async def get(self, purchase_id: UUID) -> Purchase | None:
        """Return the purchase with this id, or ``None``."""
        ...

    async def get_by_external_id(
        self,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase | None:
        """Return the purchase behind a provider invoice id, or ``None``."""
        ...

    async def get_by_charge_id(self, telegram_charge_id: str) -> Purchase | None:
        """Return the purchase carrying this Telegram charge id, or ``None``."""
        ...

    async def find_access_granting(self, user_id: int, product_id: UUID) -> Purchase | None:
        """Return the paid or delivered purchase of this product by this user."""
        ...

    async def mark_paid(
        self,
        purchase_id: UUID,
        *,
        paid_at: datetime | None = None,
        telegram_charge_id: str | None = None,
    ) -> Purchase:
        """Move a pending purchase to ``paid``.

        Idempotent: an already paid or delivered purchase is returned unchanged.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase was refunded or expired.
        """
        ...

    async def mark_delivered(
        self,
        purchase_id: UUID,
        *,
        delivered_url: str,
        delivered_at: datetime | None = None,
    ) -> Purchase:
        """Record that the delivery link was sent. Idempotent.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        ...

    async def mark_refunded(self, purchase_id: UUID) -> Purchase:
        """Revoke access after a Telegram Stars refund.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
        """
        ...

    async def expire_pending(self, older_than: datetime) -> int:
        """Mark stale pending purchases as expired; return how many changed."""
        ...

    async def list_pending(
        self,
        provider: PaymentProvider,
        *,
        limit: int = 100,
    ) -> tuple[Purchase, ...]:
        """Pending purchases of one provider, oldest first (reconciliation)."""
        ...

    async def search(self, filters: PurchaseFilters, page: PageRequest) -> Page[PurchaseRecord]:
        """Search purchases by user, product, invoice or transaction id."""
        ...


class StatsRepository(Protocol):
    """Read-only aggregates for the admin dashboard."""

    async def revenue(self, period: StatsPeriod, *, now: datetime | None = None) -> RevenueSummary:
        """Revenue and volume inside a reporting window."""
        ...

    async def top_products(
        self,
        period: StatsPeriod,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[TopProduct, ...]:
        """Best selling products inside a reporting window."""
        ...

    async def recent_purchases(self, *, limit: int = 10) -> tuple[PurchaseRecord, ...]:
        """Latest paid or delivered purchases, newest first."""
        ...
