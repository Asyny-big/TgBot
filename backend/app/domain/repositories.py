"""Repository contracts.

Structural (``Protocol``) interfaces: the domain declares what it needs, the
infrastructure layer satisfies it without importing anything from here at
runtime. Services depend on these types only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from app.domain.commands import (
        BonusTransactionDraft,
        ProductDraft,
        ProductUpdate,
        PurchaseDraft,
        ReferralDraft,
        UserDraft,
    )
    from app.domain.entities import (
        BonusTransaction,
        Product,
        Purchase,
        PurchaseRecord,
        Referral,
        ReferralRecord,
        User,
    )
    from app.domain.enums import BonusTransactionType, PaymentProvider, PurchaseStatus
    from app.domain.pagination import (
        Page,
        PageRequest,
        ProductFilters,
        PurchaseFilters,
        ReferralFilters,
    )
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

    async def get_by_referral_code(self, code: str) -> User | None:
        """Return the owner of this referral code, or ``None``."""
        ...

    async def ensure_referral_code(self, telegram_id: int, *, candidate: str) -> str:
        """Return the user's permanent referral code, assigning one if needed.

        A user has exactly one code for life. ``candidate`` is used only when
        the user has none yet; a collision with an existing code is reported so
        the caller can offer another candidate.

        Raises:
            UserNotFoundError: the user was never recorded.
            ConflictError: ``candidate`` is already taken by somebody else.
        """
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

    async def has_history(
        self,
        user_id: int,
        *,
        statuses: Sequence[PurchaseStatus],
    ) -> bool:
        """Whether this buyer has ever reached one of these statuses.

        Used to decide referral eligibility: a buyer with purchase history is
        not a new buyer, however new their Telegram account looks.
        """
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


class ReferralRepository(Protocol):
    """Persistence contract for referral relationships."""

    async def create(self, draft: ReferralDraft) -> Referral:
        """Attribute a buyer to their inviter, permanently.

        Raises:
            SelfReferralError: both sides are the same account.
            ReferralAlreadySetError: this buyer already has a referrer.
        """
        ...

    async def get(self, referral_id: UUID) -> Referral | None:
        """Return the relationship with this id, or ``None``."""
        ...

    async def get_by_referred(self, referred_user_id: int) -> Referral | None:
        """Return the relationship that owns this buyer, or ``None``."""
        ...

    async def mark_discount_used(
        self,
        referral_id: UUID,
        *,
        purchase_id: UUID,
        used_at: datetime | None = None,
    ) -> Referral:
        """Burn the one-time first-purchase discount. Idempotent.

        A second call with the *same* purchase is a replayed notification and
        changes nothing. A call with a different purchase is rejected: the
        discount exists once.

        Raises:
            ConflictError: the discount was already used by another purchase.
        """
        ...

    async def count_invited(self, referrer_user_id: int) -> int:
        """How many buyers this user has invited."""
        ...

    async def count_referral_purchases(self, referrer_user_id: int) -> int:
        """How many settled purchases this user's invitees have made."""
        ...

    async def search(self, filters: ReferralFilters, page: PageRequest) -> Page[ReferralRecord]:
        """Referral relationships with both parties, for the admin panel."""
        ...


class BonusRepository(Protocol):
    """Persistence contract for the bonus ledger and the cached balance.

    The ledger is the truth and the ``users.bonus_balance`` column is a cache of
    its sum. Both are written in the same transaction, so they cannot drift, and
    ``recompute_balance`` can always prove it.
    """

    async def balance(self, user_id: int) -> int:
        """Cached balance of this user, or ``0`` for an unknown user."""
        ...

    async def lock_balance(self, user_id: int) -> int:
        """Read the balance with ``SELECT ... FOR UPDATE``.

        Serialises concurrent spending inside the database, so two checkouts
        cannot both be told the same units are available.

        Raises:
            UserNotFoundError: the user was never recorded.
        """
        ...

    async def add(self, draft: BonusTransactionDraft) -> BonusTransaction | None:
        """Append an entry and move the cached balance in the same statement.

        Returns ``None`` when an entry of this type already exists for this
        purchase: that is a replayed notification, not an error.

        Raises:
            InsufficientBonusBalanceError: the entry would drive the balance
                below zero.
        """
        ...

    async def find_for_purchase(
        self,
        purchase_id: UUID,
        *types: BonusTransactionType,
    ) -> BonusTransaction | None:
        """Return the first matching entry recorded against this purchase."""
        ...

    async def settle_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Turn a reservation into a spend. Idempotent, and never re-debits."""
        ...

    async def release_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Give back a reservation for an invoice that was never paid.

        Idempotent: a reservation is released at most once, which the unique
        constraint on ``(purchase_id, type)`` guarantees.
        """
        ...

    async def recompute_balance(self, user_id: int) -> int:
        """Sum the ledger from scratch, ignoring the cache."""
        ...

    async def reward_total(self, user_id: int) -> int:
        """Total bonus units this user has ever earned from referrals."""
        ...

    async def purchases_awaiting_reward(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Delivered purchases by invited buyers that carry no reward yet.

        The safety net behind reward accrual: a process that died between
        marking a sale delivered and crediting the inviter is repaired here,
        the same way a lost payment webhook is repaired by reconciliation.
        """
        ...

    async def stale_holds(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Purchases holding bonus units that can no longer be paid for."""
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
