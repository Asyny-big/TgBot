"""In-memory doubles for fast service tests.

They implement the same repository contracts as the SQLAlchemy versions,
including the rules the database enforces (unique slug, unique invoice, one paid
copy per buyer), so a service test that passes here is not passing by accident.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from app.core.exceptions import (
    ConflictError,
    DuplicatePurchaseError,
    LockBusyError,
    ProductNotFoundError,
    PurchaseNotFoundError,
    SlugAlreadyExistsError,
)
from app.domain.entities import Product, Purchase, PurchaseRecord, User
from app.domain.enums import ACCESS_GRANTING_STATUSES, PurchaseStatus
from app.domain.pagination import Page
from app.domain.patch import is_set
from app.domain.stats import RevenueSummary, StatsPeriod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.domain.commands import ProductDraft, ProductUpdate, PurchaseDraft, UserDraft
    from app.domain.delivery import DeliveryMessage
    from app.domain.enums import PaymentProvider
    from app.domain.pagination import PageRequest, ProductFilters, PurchaseFilters
    from app.domain.stats import TopProduct

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class FakeProductRepository:
    """Products in a dictionary, with the unique-slug rule enforced."""

    def __init__(self) -> None:
        self.items: dict[UUID, Product] = {}
        self.purchases: FakePurchaseRepository | None = None

    async def get(self, product_id: UUID) -> Product | None:
        return self.items.get(product_id)

    async def get_by_slug(self, slug: str, *, only_active: bool = False) -> Product | None:
        for product in self.items.values():
            if product.slug == slug and (product.is_active or not only_active):
                return product
        return None

    async def list_products(self, filters: ProductFilters, page: PageRequest) -> Page[Product]:
        matches = [
            product
            for product in self.items.values()
            if (filters.search is None or filters.search.lower() in product.title.lower())
            and (filters.is_active is None or product.is_active is filters.is_active)
        ]
        matches.sort(key=lambda product: product.created_at, reverse=True)
        window = matches[page.offset : page.offset + page.limit]
        return Page(items=tuple(window), total=len(matches), limit=page.limit, offset=page.offset)

    async def create(self, draft: ProductDraft) -> Product:
        if await self.get_by_slug(draft.slug) is not None:
            raise SlugAlreadyExistsError(slug=draft.slug)
        product = Product(
            id=uuid4(),
            slug=draft.slug,
            title=draft.title,
            description=draft.description,
            photo_file_id=draft.photo_file_id,
            delivery_url=draft.delivery_url,
            price_stars=draft.price_stars,
            price_usdt=draft.price_usdt,
            is_active=draft.is_active,
            created_at=NOW,
            updated_at=NOW,
        )
        self.items[product.id] = product
        return product

    async def update(self, product_id: UUID, changes: ProductUpdate) -> Product:
        current = self.items.get(product_id)
        if current is None:
            raise ProductNotFoundError(product_id=str(product_id))
        applied = changes.changes()
        if is_set(changes.slug):
            clash = await self.get_by_slug(changes.slug)
            if clash is not None and clash.id != product_id:
                raise SlugAlreadyExistsError(slug=changes.slug)
        updated = replace(current, **applied)  # type: ignore[arg-type]
        self.items[product_id] = updated
        return updated

    async def delete(self, product_id: UUID) -> None:
        if product_id not in self.items:
            raise ProductNotFoundError(product_id=str(product_id))
        if self.purchases is not None and any(
            purchase.product_id == product_id for purchase in self.purchases.items.values()
        ):
            message = "Product has purchases and cannot be deleted; deactivate it instead"
            raise ConflictError(message, product_id=str(product_id))
        del self.items[product_id]

    async def count(self, *, only_active: bool | None = None) -> int:
        if only_active is None:
            return len(self.items)
        return sum(1 for product in self.items.values() if product.is_active is only_active)


class FakeUserRepository:
    """Users in a dictionary."""

    def __init__(self) -> None:
        self.items: dict[int, User] = {}

    async def get(self, telegram_id: int) -> User | None:
        return self.items.get(telegram_id)

    async def upsert(self, draft: UserDraft, *, seen_at: datetime | None = None) -> User:
        moment = seen_at or NOW
        existing = self.items.get(draft.telegram_id)
        user = User(
            telegram_id=draft.telegram_id,
            username=draft.username,
            first_name=draft.first_name,
            language_code=draft.language_code,
            created_at=existing.created_at if existing else moment,
            last_seen_at=moment,
        )
        self.items[user.telegram_id] = user
        return user

    async def count(self) -> int:
        return len(self.items)


class FakePurchaseRepository:
    """Purchases in a dictionary, with the invoice and access rules enforced."""

    def __init__(self) -> None:
        self.items: dict[UUID, Purchase] = {}

    async def create(self, draft: PurchaseDraft) -> Purchase:
        for existing in self.items.values():
            if existing.provider is draft.provider and existing.external_id == draft.external_id:
                message = "This invoice is already recorded"
                raise ConflictError(message, external_id=draft.external_id)
        purchase = Purchase(
            id=uuid4(),
            user_id=draft.user_id,
            product_id=draft.product_id,
            provider=draft.provider,
            status=draft.status or PurchaseStatus.PENDING,
            amount=draft.amount,
            currency=draft.currency,
            external_id=draft.external_id,
            telegram_charge_id=None,
            delivered_url=None,
            created_at=NOW,
            paid_at=None,
            delivered_at=None,
        )
        self.items[purchase.id] = purchase
        return purchase

    async def get(self, purchase_id: UUID) -> Purchase | None:
        return self.items.get(purchase_id)

    async def get_by_external_id(
        self,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase | None:
        for purchase in self.items.values():
            if purchase.provider is provider and purchase.external_id == external_id:
                return purchase
        return None

    async def get_by_charge_id(self, telegram_charge_id: str) -> Purchase | None:
        for purchase in self.items.values():
            if purchase.telegram_charge_id == telegram_charge_id:
                return purchase
        return None

    async def find_access_granting(self, user_id: int, product_id: UUID) -> Purchase | None:
        for purchase in self.items.values():
            if (
                purchase.user_id == user_id
                and purchase.product_id == product_id
                and purchase.status in ACCESS_GRANTING_STATUSES
            ):
                return purchase
        return None

    async def mark_paid(
        self,
        purchase_id: UUID,
        *,
        paid_at: datetime | None = None,
        telegram_charge_id: str | None = None,
    ) -> Purchase:
        purchase = self._require(purchase_id)
        if purchase.status in ACCESS_GRANTING_STATUSES:
            return purchase
        if purchase.status is PurchaseStatus.REFUNDED:
            message = "Refunded purchase cannot be paid again"
            raise ConflictError(message, purchase_id=str(purchase_id))
        owned = await self.find_access_granting(purchase.user_id, purchase.product_id)
        if owned is not None:
            raise DuplicatePurchaseError(purchase_id=str(purchase_id))
        updated = replace(
            purchase,
            status=PurchaseStatus.PAID,
            paid_at=paid_at or NOW,
            telegram_charge_id=telegram_charge_id or purchase.telegram_charge_id,
        )
        self.items[purchase_id] = updated
        return updated

    async def mark_delivered(
        self,
        purchase_id: UUID,
        *,
        delivered_url: str,
        delivered_at: datetime | None = None,
    ) -> Purchase:
        purchase = self._require(purchase_id)
        if purchase.status is PurchaseStatus.DELIVERED:
            return purchase
        if purchase.status is not PurchaseStatus.PAID:
            message = "Only a paid purchase can be delivered"
            raise ConflictError(message, purchase_id=str(purchase_id))
        updated = replace(
            purchase,
            status=PurchaseStatus.DELIVERED,
            delivered_url=delivered_url,
            delivered_at=delivered_at or NOW,
        )
        self.items[purchase_id] = updated
        return updated

    async def mark_refunded(self, purchase_id: UUID) -> Purchase:
        purchase = self._require(purchase_id)
        updated = replace(purchase, status=PurchaseStatus.REFUNDED)
        self.items[purchase_id] = updated
        return updated

    async def expire_pending(self, older_than: datetime) -> int:
        expired = 0
        for purchase_id, purchase in list(self.items.items()):
            if purchase.status is PurchaseStatus.PENDING and purchase.created_at < older_than:
                self.items[purchase_id] = replace(purchase, status=PurchaseStatus.EXPIRED)
                expired += 1
        return expired

    async def list_pending(
        self,
        provider: PaymentProvider,
        *,
        limit: int = 100,
    ) -> tuple[Purchase, ...]:
        pending = [
            purchase
            for purchase in self.items.values()
            if purchase.provider is provider and purchase.status is PurchaseStatus.PENDING
        ]
        pending.sort(key=lambda purchase: purchase.created_at)
        return tuple(pending[:limit])

    async def search(self, filters: PurchaseFilters, page: PageRequest) -> Page[PurchaseRecord]:
        del filters
        return Page(items=(), total=0, limit=page.limit, offset=page.offset)

    def _require(self, purchase_id: UUID) -> Purchase:
        purchase = self.items.get(purchase_id)
        if purchase is None:
            raise PurchaseNotFoundError(purchase_id=str(purchase_id))
        return purchase


@dataclass
class FakeStatsRepository:
    """Returns canned aggregates so the stats service can be tested alone."""

    revenues: dict[StatsPeriod, RevenueSummary] = field(default_factory=dict)
    top: tuple[TopProduct, ...] = ()
    recent: tuple[PurchaseRecord, ...] = ()

    async def revenue(
        self,
        period: StatsPeriod,
        *,
        now: datetime | None = None,
    ) -> RevenueSummary:
        del now
        return self.revenues.get(
            period,
            RevenueSummary(
                period=period,
                purchases_count=0,
                stars_amount=0,
                usdt_amount=Decimal(0),
            ),
        )

    async def top_products(
        self,
        period: StatsPeriod,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[TopProduct, ...]:
        del period, now
        return self.top[:limit]

    async def recent_purchases(self, *, limit: int = 10) -> tuple[PurchaseRecord, ...]:
        return self.recent[:limit]


class FakeUnitOfWork:
    """One shared set of in-memory repositories."""

    def __init__(self) -> None:
        self._products = FakeProductRepository()
        self._users = FakeUserRepository()
        self._purchases = FakePurchaseRepository()
        self._stats = FakeStatsRepository()
        self._products.purchases = self._purchases
        self.commits = 0
        self.rollbacks = 0

    @property
    def products(self) -> FakeProductRepository:
        return self._products

    @property
    def users(self) -> FakeUserRepository:
        return self._users

    @property
    def purchases(self) -> FakePurchaseRepository:
        return self._purchases

    @property
    def stats(self) -> FakeStatsRepository:
        return self._stats

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeUnitOfWorkFactory:
    """Hands the same unit of work to every scope, counting the scopes."""

    def __init__(self, unit: FakeUnitOfWork | None = None) -> None:
        self.unit = unit or FakeUnitOfWork()
        self.scopes = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeUnitOfWork]:
        self.scopes += 1
        try:
            yield self.unit
        except Exception:
            await self.unit.rollback()
            raise
        await self.unit.commit()


class FakeLockManager:
    """In-memory locks that honour a TTL, using a monotonic clock."""

    def __init__(self, *, clock: Any = monotonic) -> None:
        self._clock = clock
        self.held: dict[str, float] = {}
        self.acquired: list[tuple[str, float | None]] = []
        self.busy_keys: set[str] = set()

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        wait_seconds: float = 0.0,
    ) -> AsyncIterator[None]:
        del wait_seconds
        expiry = self.held.get(key)
        if key in self.busy_keys or (expiry is not None and expiry > self._clock()):
            raise LockBusyError(lock_key=key)
        self.acquired.append((key, ttl_seconds))
        self.held[key] = self._clock() + (ttl_seconds or 45.0)
        try:
            yield
        finally:
            self.held.pop(key, None)


class FakeDeliveryGateway:
    """Programmable transport: raises the queued errors, then succeeds."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.sent: list[DeliveryMessage] = []

    async def send(self, message: DeliveryMessage) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)


class FakeRevocationStore:
    """Token revocation list in a set."""

    def __init__(self) -> None:
        self.revoked: dict[str, float] = {}

    async def revoke(self, token_id: str, *, ttl_seconds: float) -> None:
        self.revoked[token_id] = ttl_seconds

    async def is_revoked(self, token_id: str) -> bool:
        return token_id in self.revoked


class RecordingSleeper:
    """Captures the delays a retry policy asks for, without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
