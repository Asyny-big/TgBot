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
    CheckoutAlreadyPendingError,
    ConflictError,
    DailyContentDuplicateError,
    DiscountAlreadyUsedError,
    DuplicatePurchaseError,
    InsufficientBonusBalanceError,
    LockBusyError,
    ProductNotFoundError,
    PurchaseNotFoundError,
    ReferralAlreadySetError,
    ReferralNotFoundError,
    SelfReferralError,
    SlugAlreadyExistsError,
    UserNotFoundError,
)
from app.domain.bonuses import BonusPolicy, reward_bonuses
from app.domain.commands import BonusTransactionDraft
from app.domain.daily import LibraryStats
from app.domain.entities import (
    BonusTransaction,
    DailyContent,
    DailyDelivery,
    DailyDeliveryAttempt,
    DailySubscription,
    LoyaltyReward,
    Product,
    Purchase,
    PurchaseRecord,
    Referral,
    ReferralRecord,
    User,
)
from app.domain.enums import (
    ACCESS_GRANTING_STATUSES,
    BONUS_HOLD_TYPES,
    BonusTransactionType,
    Currency,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailySubscriptionStatus,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import REWARD_MILESTONES
from app.domain.pagination import Page
from app.domain.patch import is_set
from app.domain.stats import RevenueSummary, StatsPeriod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from datetime import date

    from app.domain.commands import (
        DailyContentDraft,
        DailyDeliveryDraft,
        ProductDraft,
        ProductUpdate,
        PurchaseDraft,
        ReferralDraft,
        UserDraft,
    )
    from app.domain.delivery import DeliveryMessage
    from app.domain.pagination import (
        PageRequest,
        ProductFilters,
        PurchaseFilters,
        ReferralFilters,
    )
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
            # An upsert refreshes the Telegram profile snapshot only: it must
            # never clobber the referral code or the balance, exactly like the
            # ON CONFLICT clause in the real repository.
            referral_code=existing.referral_code if existing else None,
            bonus_balance=existing.bonus_balance if existing else 0,
        )
        self.items[user.telegram_id] = user
        return user

    async def count(self) -> int:
        return len(self.items)

    async def get_by_referral_code(self, code: str) -> User | None:
        for user in self.items.values():
            if user.referral_code == code:
                return user
        return None

    async def ensure_referral_code(self, telegram_id: int, *, candidate: str) -> str:
        user = self.items.get(telegram_id)
        if user is None:
            raise UserNotFoundError(telegram_id=telegram_id)
        if user.referral_code:
            return user.referral_code
        if await self.get_by_referral_code(candidate) is not None:
            message = "This referral code is already taken"
            raise ConflictError(message, candidate=candidate)
        self.items[telegram_id] = replace(user, referral_code=candidate)
        return candidate

    def move_balance(self, telegram_id: int, delta: int) -> int:
        """Apply a delta to the cached balance, refusing to go negative.

        Mirrors ``ck_users_bonus_balance_non_negative``: the fake enforces the
        constraint so a service test cannot pass by ignoring it.
        """
        user = self.items.get(telegram_id)
        if user is None:
            raise UserNotFoundError(telegram_id=telegram_id)
        updated = user.bonus_balance + delta
        if updated < 0:
            raise InsufficientBonusBalanceError(user_id=telegram_id, requested=abs(delta))
        self.items[telegram_id] = replace(user, bonus_balance=updated)
        return updated


class FakePurchaseRepository:
    """Purchases in a dictionary, with the invoice and access rules enforced."""

    def __init__(self) -> None:
        self.items: dict[UUID, Purchase] = {}

    async def create(self, draft: PurchaseDraft) -> Purchase:
        self._check_amount_rules(draft)
        for existing in self.items.values():
            if existing.provider is draft.provider and existing.external_id == draft.external_id:
                message = "This invoice is already recorded"
                raise ConflictError(message, external_id=draft.external_id)
            if (
                draft.provider is PaymentProvider.CRYPTO
                and (draft.status or PurchaseStatus.PENDING) is PurchaseStatus.PENDING
                and existing.provider is PaymentProvider.CRYPTO
                and existing.status is PurchaseStatus.PENDING
                and (existing.user_id, existing.product_id) == (draft.user_id, draft.product_id)
            ):
                # Mirrors uq_purchases_user_product_pending_crypto.
                raise CheckoutAlreadyPendingError(external_id=draft.external_id)
        purchase = Purchase(
            id=uuid4(),
            user_id=draft.user_id,
            product_id=draft.product_id,
            provider=draft.provider,
            status=draft.status or PurchaseStatus.PENDING,
            amount=draft.amount,
            base_amount=draft.base_amount,
            discount_amount=draft.discount_amount,
            bonus_amount=draft.bonus_amount,
            loyalty_amount=draft.loyalty_amount,
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

    async def get_for_update(self, purchase_id: UUID) -> Purchase | None:
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

    async def has_history(
        self,
        user_id: int,
        *,
        statuses: Sequence[PurchaseStatus],
    ) -> bool:
        return any(
            purchase.user_id == user_id and purchase.status in statuses
            for purchase in self.items.values()
        )

    async def has_pending_discount(self, user_id: int) -> bool:
        return any(
            purchase.user_id == user_id
            and purchase.status is PurchaseStatus.PENDING
            and purchase.discount_amount > 0
            for purchase in self.items.values()
        )

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

    async def expire(self, purchase_id: UUID) -> Purchase:
        purchase = self._require(purchase_id)
        if purchase.status is not PurchaseStatus.PENDING:
            return purchase
        expired = replace(purchase, status=PurchaseStatus.EXPIRED)
        self.items[purchase_id] = expired
        return expired

    async def pending_checkouts(
        self,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
    ) -> tuple[Purchase, ...]:
        pending = [
            purchase
            for purchase in self.items.values()
            if purchase.user_id == user_id
            and purchase.product_id == product_id
            and purchase.provider is provider
            and purchase.status is PurchaseStatus.PENDING
        ]
        # Insertion order stands in for created_at, which is constant in the fakes.
        return tuple(reversed(pending))

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

    @staticmethod
    def _check_amount_rules(draft: PurchaseDraft) -> None:
        """Mirror ck_purchases_amount_matches_provider and _internal_is_loyalty_grant."""
        status = draft.status or PurchaseStatus.PENDING
        if draft.provider is PaymentProvider.INTERNAL:
            valid = (
                draft.amount == 0
                and draft.base_amount is not None
                and draft.base_amount > 0
                and draft.loyalty_amount == draft.base_amount
                and draft.discount_amount == 0
                and draft.bonus_amount == 0
                and status in (PurchaseStatus.PAID, PurchaseStatus.DELIVERED)
            )
        else:
            valid = draft.amount > 0
        if not valid:
            message = "Purchase amount does not match its provider"
            raise ValueError(message)


class FakeReferralRepository:
    """Referrals in a dictionary, with the permanence rules enforced.

    The two constraints the database carries are reproduced here on purpose: a
    buyer has at most one referrer for life, and a discount is burned by at most
    one purchase. A service test that violated either would fail here too.
    """

    def __init__(self) -> None:
        self.items: dict[UUID, Referral] = {}
        self.purchases: FakePurchaseRepository | None = None

    async def create(self, draft: ReferralDraft) -> Referral:
        if draft.referrer_user_id == draft.referred_user_id:
            raise SelfReferralError(user_id=draft.referred_user_id)
        if await self.get_by_referred(draft.referred_user_id) is not None:
            raise ReferralAlreadySetError(referred_user_id=draft.referred_user_id)
        referral = Referral(
            id=uuid4(),
            referrer_user_id=draft.referrer_user_id,
            referred_user_id=draft.referred_user_id,
            created_at=NOW,
        )
        self.items[referral.id] = referral
        return referral

    async def get(self, referral_id: UUID) -> Referral | None:
        return self.items.get(referral_id)

    async def get_by_referred(self, referred_user_id: int) -> Referral | None:
        for referral in self.items.values():
            if referral.referred_user_id == referred_user_id:
                return referral
        return None

    async def lock_for_discount_check(self, referred_user_id: int) -> Referral | None:
        return await self.get_by_referred(referred_user_id)

    async def mark_discount_used(
        self,
        referral_id: UUID,
        *,
        purchase_id: UUID,
        used_at: datetime | None = None,
    ) -> Referral:
        referral = self.items.get(referral_id)
        if referral is None:
            raise ReferralNotFoundError(referral_id=str(referral_id))
        if referral.discount_purchase_id is not None:
            if referral.discount_purchase_id == purchase_id:
                return referral
            raise DiscountAlreadyUsedError(referral_id=str(referral_id))
        updated = replace(
            referral,
            discount_purchase_id=purchase_id,
            discount_used_at=used_at or NOW,
        )
        self.items[referral_id] = updated
        return updated

    async def count_invited(self, referrer_user_id: int) -> int:
        return sum(
            1 for referral in self.items.values() if referral.referrer_user_id == referrer_user_id
        )

    async def count_referral_purchases(self, referrer_user_id: int) -> int:
        if self.purchases is None:
            return 0
        invited = {
            referral.referred_user_id
            for referral in self.items.values()
            if referral.referrer_user_id == referrer_user_id
        }
        return sum(
            1
            for purchase in self.purchases.items.values()
            if purchase.user_id in invited
            and purchase.status in ACCESS_GRANTING_STATUSES
            and purchase.provider.is_payable
        )

    async def search(self, filters: ReferralFilters, page: PageRequest) -> Page[ReferralRecord]:
        del filters
        return Page(items=(), total=0, limit=page.limit, offset=page.offset)


class FakeBonusRepository:
    """A bonus ledger in a list, with the idempotency rules enforced.

    ``(purchase_id, type)`` is unique here as well, which is what lets a test
    replay a payment notification five times and assert one reward.
    """

    def __init__(self, users: FakeUserRepository) -> None:
        self.entries: list[BonusTransaction] = []
        self.users = users
        self.purchases: FakePurchaseRepository | None = None
        self.products: FakeProductRepository | None = None

    async def balance(self, user_id: int) -> int:
        user = self.users.items.get(user_id)
        return user.bonus_balance if user is not None else 0

    async def lock_balance(self, user_id: int) -> int:
        if user_id not in self.users.items:
            raise UserNotFoundError(telegram_id=user_id)
        return await self.balance(user_id)

    async def add(self, draft: BonusTransactionDraft) -> BonusTransaction | None:
        if draft.purchase_id is not None and any(
            entry.purchase_id == draft.purchase_id and entry.type is draft.type
            for entry in self.entries
        ):
            return None
        self.users.move_balance(draft.user_id, draft.amount)
        entry = BonusTransaction(
            id=uuid4(),
            user_id=draft.user_id,
            amount=draft.amount,
            type=draft.type,
            referral_id=draft.referral_id,
            purchase_id=draft.purchase_id,
            stars_per_usdt=draft.stars_per_usdt,
            created_at=NOW,
        )
        self.entries.append(entry)
        return entry

    async def find_for_purchase(
        self,
        purchase_id: UUID,
        *types: BonusTransactionType,
    ) -> BonusTransaction | None:
        for entry in self.entries:
            if entry.purchase_id == purchase_id and (not types or entry.type in types):
                return entry
        return None

    async def settle_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        for index, entry in enumerate(self.entries):
            if entry.purchase_id == purchase_id and entry.type in BONUS_HOLD_TYPES:
                if entry.type is BonusTransactionType.BONUS_SPENT:
                    return entry
                settled = replace(entry, type=BonusTransactionType.BONUS_SPENT)
                self.entries[index] = settled
                return settled
        return None

    async def release_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        held = await self.find_for_purchase(purchase_id, *BONUS_HOLD_TYPES)
        if held is None or held.type is BonusTransactionType.BONUS_SPENT:
            return None
        return await self.add(
            BonusTransactionDraft(
                user_id=held.user_id,
                amount=abs(held.amount),
                type=BonusTransactionType.BONUS_RESERVE_RELEASED,
                purchase_id=purchase_id,
                referral_id=held.referral_id,
            )
        )

    async def recompute_balance(self, user_id: int) -> int:
        return sum(entry.amount for entry in self.entries if entry.user_id == user_id)

    async def reward_total(self, user_id: int) -> int:
        return sum(
            entry.amount
            for entry in self.entries
            if entry.user_id == user_id and entry.type is BonusTransactionType.REFERRAL_REWARD
        )

    async def purchases_awaiting_reward(
        self,
        *,
        reward_percent: int,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        if self.purchases is None or reward_percent <= 0:
            return ()
        rewarded = {
            entry.purchase_id
            for entry in self.entries
            if entry.type is BonusTransactionType.REFERRAL_REWARD
        }
        pending = [
            purchase.id
            for purchase in self.purchases.items.values()
            if purchase.status is PurchaseStatus.DELIVERED
            and purchase.id not in rewarded
            and self._earns_a_reward(purchase, reward_percent)
        ]
        return tuple(pending[:limit])

    def _earns_a_reward(self, purchase: Purchase, reward_percent: int) -> bool:
        """The reward formula itself, standing in for the SQL predicate."""
        product = self.products.items.get(purchase.product_id) if self.products else None
        rate = product.stars_per_usdt if product is not None else None
        units = reward_bonuses(
            purchase.amount,
            purchase.currency,
            policy=BonusPolicy(reward_percent=reward_percent),
            stars_per_usdt=None if purchase.currency is Currency.XTR else rate,
        )
        return units > 0

    async def stale_holds(self, *, limit: int = 100) -> tuple[UUID, ...]:
        if self.purchases is None:
            return ()
        dead = (PurchaseStatus.EXPIRED, PurchaseStatus.REFUNDED)
        released = {
            entry.purchase_id
            for entry in self.entries
            if entry.type is BonusTransactionType.BONUS_RESERVE_RELEASED
        }
        held = [
            entry.purchase_id
            for entry in self.entries
            if entry.type is BonusTransactionType.BONUS_RESERVED
            and entry.purchase_id is not None
            and entry.purchase_id not in released
            and (purchase := self.purchases.items.get(entry.purchase_id)) is not None
            and purchase.status in dead
        ]
        return tuple(item for item in held[:limit] if item is not None)


class FakeLoyaltyRepository:
    """Loyalty read model over the fake purchases, counting exactly as the SQL does."""

    def __init__(self, purchases: FakePurchaseRepository) -> None:
        self.purchases = purchases
        self.rewards: list[LoyaltyReward] = []
        self.progress_locks: list[int] = []
        """Buyers whose progress lock was taken, in order. One process: no waiting."""

    async def count_completed(self, user_id: int) -> int:
        return sum(
            1
            for purchase in self.purchases.items.values()
            if purchase.user_id == user_id
            and (
                purchase.status is PurchaseStatus.DELIVERED
                or (
                    purchase.status is PurchaseStatus.REFUNDED and purchase.delivered_at is not None
                )
            )
        )

    async def rewards_of(self, user_id: int) -> tuple[LoyaltyReward, ...]:
        return tuple(
            sorted(
                (reward for reward in self.rewards if reward.user_id == user_id),
                key=lambda reward: reward.milestone,
            )
        )

    async def ensure_reward(self, user_id: int, milestone: int) -> None:
        if any(r.user_id == user_id and r.milestone == milestone for r in self.rewards):
            return
        self.rewards.append(
            LoyaltyReward(
                id=uuid4(),
                user_id=user_id,
                milestone=milestone,
                kind=REWARD_MILESTONES[milestone],
                status=LoyaltyRewardStatus.AVAILABLE,
                earned_at=NOW,
            )
        )

    async def lock_reward(self, user_id: int, milestone: int) -> LoyaltyReward | None:
        for reward in self.rewards:
            if reward.user_id == user_id and reward.milestone == milestone:
                return reward
        return None

    async def mark_applied(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        reward = self._require(reward_id)
        if reward.status is not LoyaltyRewardStatus.AVAILABLE:
            message = "The loyalty reward is not available"
            raise ConflictError(message, reward_id=str(reward_id))
        if any(r.applied_purchase_id == purchase_id for r in self.rewards):
            message = "The loyalty reward could not change state"
            raise ConflictError(message, reward_id=str(reward_id))
        return self._store(
            replace(
                reward,
                status=LoyaltyRewardStatus.APPLIED,
                applied_purchase_id=purchase_id,
                applied_at=NOW,
            )
        )

    async def mark_consumed(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        reward = self._require(reward_id)
        if reward.status is LoyaltyRewardStatus.CONSUMED:
            if reward.consumed_purchase_id == purchase_id:
                return reward
            message = "The loyalty reward was already consumed by another purchase"
            raise ConflictError(message, reward_id=str(reward_id))
        return self._store(
            replace(
                reward,
                status=LoyaltyRewardStatus.CONSUMED,
                consumed_purchase_id=purchase_id,
                consumed_at=NOW,
            )
        )

    async def mark_released(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward | None:
        reward = self._require(reward_id)
        if (
            reward.status is not LoyaltyRewardStatus.APPLIED
            or reward.applied_purchase_id != purchase_id
        ):
            return None
        return self._store(
            replace(
                reward,
                status=LoyaltyRewardStatus.AVAILABLE,
                applied_purchase_id=None,
                applied_at=None,
            )
        )

    async def release_expired(self, *, limit: int = 100) -> tuple[UUID, ...]:
        released: list[UUID] = []
        for reward in list(self.rewards):
            holder = (
                self.purchases.items.get(reward.applied_purchase_id)
                if reward.applied_purchase_id is not None
                else None
            )
            if (
                reward.status is LoyaltyRewardStatus.APPLIED
                and holder is not None
                and holder.status is PurchaseStatus.EXPIRED
                and len(released) < limit
            ):
                self._store(
                    replace(
                        reward,
                        status=LoyaltyRewardStatus.AVAILABLE,
                        applied_purchase_id=None,
                        applied_at=None,
                    )
                )
                released.append(reward.id)
        return tuple(released)

    async def lock_progress(self, user_id: int) -> None:
        self.progress_locks.append(user_id)

    def _require(self, reward_id: UUID) -> LoyaltyReward:
        for reward in self.rewards:
            if reward.id == reward_id:
                return reward
        message = "No such loyalty reward"
        raise ConflictError(message, reward_id=str(reward_id))

    def _store(self, updated: LoyaltyReward) -> LoyaltyReward:
        self.rewards = [updated if r.id == updated.id else r for r in self.rewards]
        return updated


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
        self._referrals = FakeReferralRepository()
        self._bonuses = FakeBonusRepository(self._users)
        self._stats = FakeStatsRepository()
        self._loyalty = FakeLoyaltyRepository(self._purchases)
        self._daily_subscriptions = FakeDailySubscriptionRepository(self._loyalty)
        self._daily_deliveries = FakeDailyDeliveryRepository()
        self._daily_content = FakeDailyContentRepository(self._daily_deliveries)
        self._products.purchases = self._purchases
        self._referrals.purchases = self._purchases
        self._bonuses.purchases = self._purchases
        self._bonuses.products = self._products
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
    def referrals(self) -> FakeReferralRepository:
        return self._referrals

    @property
    def bonuses(self) -> FakeBonusRepository:
        return self._bonuses

    @property
    def stats(self) -> FakeStatsRepository:
        return self._stats

    @property
    def loyalty(self) -> FakeLoyaltyRepository:
        return self._loyalty

    @property
    def daily_subscriptions(self) -> FakeDailySubscriptionRepository:
        return self._daily_subscriptions

    @property
    def daily_content(self) -> FakeDailyContentRepository:
        return self._daily_content

    @property
    def daily_deliveries(self) -> FakeDailyDeliveryRepository:
        return self._daily_deliveries

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


# --------------------------------------------------------------------------- #
# Daily Content                                                               #
# --------------------------------------------------------------------------- #

_DAILY_HOLDING = (
    DailyDeliveryStatus.PENDING,
    DailyDeliveryStatus.SENDING,
    DailyDeliveryStatus.SENT,
)
_DAILY_WAITING = (DailyDeliveryStatus.PENDING, DailyDeliveryStatus.NO_CONTENT)
_DAILY_OPEN = (*_DAILY_WAITING, DailyDeliveryStatus.SENDING)


class FakeDailySubscriptionRepository:
    """Daily subscriptions in a dict, with the transitions the SQL allows.

    Access is read through the fake loyalty repository's own count, exactly as
    the SQL reads the shared completed-purchase predicate.
    """

    def __init__(self, loyalty: FakeLoyaltyRepository) -> None:
        self.loyalty = loyalty
        self.items: dict[int, DailySubscription] = {}

    async def get(self, user_id: int) -> DailySubscription | None:
        return self.items.get(user_id)

    async def lock(self, user_id: int) -> DailySubscription | None:
        return self.items.get(user_id)

    async def record_first_interaction(self, user_id: int, *, at: datetime) -> DailySubscription:
        if user_id not in self.items:
            self.items[user_id] = DailySubscription(
                user_id=user_id,
                status=DailySubscriptionStatus.INACTIVE,
                first_interaction_at=at,
                created_at=at,
                updated_at=at,
            )
        return self.items[user_id]

    async def activate(
        self,
        user_id: int,
        *,
        at: datetime,
        anchor_minute: int,
    ) -> DailySubscription:
        current = self._require(user_id)
        if current.status is not DailySubscriptionStatus.INACTIVE:
            message = "The Daily Content subscription is already activated"
            raise ConflictError(message, user_id=user_id, status=current.status.value)
        return self._store(
            replace(
                current,
                status=DailySubscriptionStatus.ACTIVE,
                activated_at=at,
                anchor_minute=anchor_minute,
                updated_at=at,
            )
        )

    async def pause(self, user_id: int, *, at: datetime) -> DailySubscription:
        current = self._require_activated(user_id)
        if current.status is DailySubscriptionStatus.PAUSED:
            return current
        return self._store(
            replace(current, status=DailySubscriptionStatus.PAUSED, paused_at=at, updated_at=at)
        )

    async def resume(self, user_id: int, *, at: datetime) -> DailySubscription:
        current = self._require_activated(user_id)
        if current.status is DailySubscriptionStatus.ACTIVE:
            return current
        return self._store(
            replace(current, status=DailySubscriptionStatus.ACTIVE, paused_at=None, updated_at=at)
        )

    async def mark_blocked(self, user_id: int, *, at: datetime) -> DailySubscription | None:
        current = self.items.get(user_id)
        if current is None:
            return None
        if current.blocked_at is not None:
            return current
        return self._store(replace(current, blocked_at=at, updated_at=at))

    async def mark_unblocked(self, user_id: int) -> DailySubscription | None:
        current = self.items.get(user_id)
        if current is None:
            return None
        return self._store(replace(current, blocked_at=None))

    async def deliverable(
        self,
        *,
        min_completed: int,
        after_user_id: int | None = None,
        limit: int = 100,
        only: tuple[int, ...] = (),
    ) -> tuple[DailySubscription, ...]:
        found: list[DailySubscription] = []
        for user_id in sorted(self.items):
            item = self.items[user_id]
            if after_user_id is not None and user_id <= after_user_id:
                continue
            if only and user_id not in only:
                continue
            if not item.receives_deliveries:
                continue
            if await self.loyalty.count_completed(user_id) < min_completed:
                continue
            found.append(item)
            if len(found) == limit:
                break
        return tuple(found)

    def _require(self, user_id: int) -> DailySubscription:
        current = self.items.get(user_id)
        if current is None:
            message = "No Daily Content subscription for this buyer"
            raise ConflictError(message, user_id=user_id)
        return current

    def _require_activated(self, user_id: int) -> DailySubscription:
        current = self._require(user_id)
        if current.status is DailySubscriptionStatus.INACTIVE:
            message = "The Daily Content subscription was never activated"
            raise ConflictError(message, user_id=user_id)
        return current

    def _store(self, item: DailySubscription) -> DailySubscription:
        self.items[item.user_id] = item
        return item


class FakeDailyContentRepository:
    """The library in a dict. ``pick_for`` takes the lowest eligible id, not a random one."""

    def __init__(self, deliveries: FakeDailyDeliveryRepository) -> None:
        self.deliveries = deliveries
        self.items: dict[int, DailyContent] = {}
        self._next_id = 1

    async def add(self, draft: DailyContentDraft) -> tuple[DailyContent, bool]:
        for item in self.items.values():
            same_message = (item.source_chat_id, item.source_message_id) == (
                draft.source_chat_id,
                draft.source_message_id,
            )
            if same_message:
                return item, False
        if draft.file_unique_id is not None:
            self._refuse_live_duplicate(draft.file_unique_id, keep=None)
        if draft.kind is DailyContentKind.TEXT and draft.body is not None:
            self._refuse_live_text_twin(draft.body, keep=None)
        now = datetime.now(UTC)
        item = DailyContent(
            id=self._next_id,
            status=DailyContentStatus.ACTIVE,
            kind=draft.kind,
            source_chat_id=draft.source_chat_id,
            source_message_id=draft.source_message_id,
            added_by=draft.added_by,
            created_at=now,
            updated_at=now,
            status_changed_at=now,
            body=draft.body,
            entities=draft.entities,
            file_id=draft.file_id,
            file_unique_id=draft.file_unique_id,
            file_size=draft.file_size,
            file_name=draft.file_name,
            mime_type=draft.mime_type,
            duration=draft.duration,
            width=draft.width,
            height=draft.height,
        )
        self._next_id += 1
        self.items[item.id] = item
        return item, True

    async def get(self, content_id: int) -> DailyContent | None:
        return self.items.get(content_id)

    async def lock(self, content_id: int) -> DailyContent | None:
        return self.items.get(content_id)

    async def find_by_file(self, file_unique_id: str) -> DailyContent | None:
        return self._find(lambda item: item.file_unique_id == file_unique_id)

    async def find_by_text(self, body: str) -> DailyContent | None:
        return self._find(lambda item: item.kind is DailyContentKind.TEXT and item.body == body)

    async def stats(self) -> LibraryStats:
        by_status = dict.fromkeys(DailyContentStatus, 0)
        active_by_kind: dict[DailyContentKind, int] = {}
        for item in self.items.values():
            by_status[item.status] += 1
            if item.is_active:
                active_by_kind[item.kind] = active_by_kind.get(item.kind, 0) + 1
        return LibraryStats(
            active=by_status[DailyContentStatus.ACTIVE],
            disabled=by_status[DailyContentStatus.DISABLED],
            archived=by_status[DailyContentStatus.ARCHIVED],
            active_by_kind=active_by_kind,
        )

    async def set_status(
        self,
        content_id: int,
        status: DailyContentStatus,
        *,
        at: datetime,
        reason: str | None = None,
    ) -> DailyContent:
        current = self.items.get(content_id)
        if current is None:
            message = "No such Daily Content item"
            raise ConflictError(message, content_id=content_id)
        if current.status is status:
            return current
        leaving_archive = (
            current.status is DailyContentStatus.ARCHIVED
            and status is not DailyContentStatus.ARCHIVED
        )
        if leaving_archive and current.file_unique_id is not None:
            self._refuse_live_duplicate(current.file_unique_id, keep=content_id)
        if leaving_archive and current.kind is DailyContentKind.TEXT and current.body is not None:
            self._refuse_live_text_twin(current.body, keep=content_id)
        updated = replace(
            current,
            status=status,
            status_changed_at=at,
            status_reason=reason,
            updated_at=at,
        )
        self.items[content_id] = updated
        return updated

    async def pick_for(self, user_id: int) -> DailyContent | None:
        held = {
            delivery.content_id
            for delivery in self.deliveries.items.values()
            if delivery.user_id == user_id
            and delivery.content_id is not None
            and delivery.status in _DAILY_HOLDING
        }
        for content_id in sorted(self.items):
            item = self.items[content_id]
            if item.is_active and content_id not in held:
                return item
        return None

    async def count_active(self) -> int:
        return sum(1 for item in self.items.values() if item.is_active)

    async def count_available_for(self, user_id: int) -> int:
        held = {
            delivery.content_id
            for delivery in self.deliveries.items.values()
            if delivery.user_id == user_id
            and delivery.content_id is not None
            and delivery.status in _DAILY_HOLDING
        }
        return sum(1 for item in self.items.values() if item.is_active and item.id not in held)

    def _refuse_live_duplicate(self, file_unique_id: str, *, keep: int | None) -> None:
        for item in self.items.values():
            live = item.status is not DailyContentStatus.ARCHIVED
            if item.id != keep and live and item.file_unique_id == file_unique_id:
                raise DailyContentDuplicateError(existing_id=item.id)

    def _refuse_live_text_twin(self, body: str, *, keep: int | None) -> None:
        for content_id in sorted(self.items):
            item = self.items[content_id]
            live = item.status is not DailyContentStatus.ARCHIVED
            text = item.kind is DailyContentKind.TEXT
            if content_id != keep and live and text and item.body == body:
                raise DailyContentDuplicateError(existing_id=content_id)

    def _find(self, matches: Callable[[DailyContent], bool]) -> DailyContent | None:
        found = [item for item in self.items.values() if matches(item)]
        live = [item for item in found if item.status is not DailyContentStatus.ARCHIVED]
        if live:
            return live[0]
        return max(found, key=lambda item: item.id) if found else None


class FakeDailyDeliveryRepository:
    """Deliveries and attempts in dicts, enforcing what the SQL enforces."""

    def __init__(self) -> None:
        self.items: dict[int, DailyDelivery] = {}
        self.attempts: dict[UUID, DailyDeliveryAttempt] = {}
        self._next_id = 1
        self._next_attempt_id = 1

    async def open(self, draft: DailyDeliveryDraft) -> DailyDelivery | None:
        window = draft.window
        for item in self.items.values():
            if (item.user_id, item.delivery_date) == (draft.user_id, window.delivery_date):
                return None
        now = datetime.now(UTC)
        item = DailyDelivery(
            id=self._next_id,
            user_id=draft.user_id,
            delivery_date=window.delivery_date,
            trigger=draft.trigger,
            status=DailyDeliveryStatus.PENDING,
            scheduled_for=window.center,
            window_opens_at=window.opens_at,
            window_closes_at=window.closes_at,
            deadline_at=window.deadline_at,
            next_attempt_at=draft.first_attempt_at,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        self._next_id += 1
        self.items[item.id] = item
        return item

    async def get(self, delivery_id: int) -> DailyDelivery | None:
        return self.items.get(delivery_id)

    async def for_date(self, user_id: int, delivery_date: date) -> DailyDelivery | None:
        for item in self.items.values():
            if (item.user_id, item.delivery_date) == (user_id, delivery_date):
                return item
        return None

    async def lock(self, delivery_id: int) -> DailyDelivery | None:
        return self.items.get(delivery_id)

    async def lock_due(self, *, now: datetime, limit: int) -> tuple[DailyDelivery, ...]:
        due = [
            item
            for item in self.items.values()
            if item.deadline_at > now
            and (
                (item.status in _DAILY_WAITING and item.next_attempt_at <= now)
                or (
                    item.status is DailyDeliveryStatus.SENDING
                    and item.lease_until is not None
                    and item.lease_until <= now
                )
            )
        ]
        due.sort(key=lambda item: (item.next_attempt_at, item.id))
        return tuple(due[:limit])

    async def start_attempt(
        self,
        delivery_id: int,
        *,
        content_id: int,
        now: datetime,
        lease_until: datetime,
    ) -> DailyDeliveryAttempt:
        item = self.items.get(delivery_id)
        if item is None:
            message = "No such Daily Content delivery"
            raise ConflictError(message, delivery_id=delivery_id)
        dead_lease = (
            item.status is DailyDeliveryStatus.SENDING
            and item.lease_until is not None
            and item.lease_until <= now
        )
        if item.status not in _DAILY_WAITING and not dead_lease:
            message = "The delivery cannot be attempted now"
            raise ConflictError(message, delivery_id=delivery_id, status=item.status.value)
        if item.content_id is not None and item.content_id != content_id:
            message = "A delivery sends the item it started with"
            raise ConflictError(message, delivery_id=delivery_id)
        for other in self.items.values():
            holds = (
                other.id != delivery_id
                and other.user_id == item.user_id
                and other.content_id == content_id
                and other.status in _DAILY_HOLDING
            )
            if holds:
                message = "The buyer already holds or received this item"
                raise ConflictError(
                    message,
                    delivery_id=delivery_id,
                    content_id=content_id,
                    constraint="uq_daily_deliveries_user_content",
                )
        if dead_lease:
            item = self._close_dangling(item, at=now)
        token = uuid4()
        item = replace(
            item,
            status=DailyDeliveryStatus.SENDING,
            content_id=content_id,
            lease_token=token,
            lease_until=lease_until,
            attempts=item.attempts + 1,
            updated_at=now,
        )
        self.items[item.id] = item
        attempt = DailyDeliveryAttempt(
            id=self._next_attempt_id,
            delivery_id=item.id,
            attempt_no=item.attempts,
            lease_token=token,
            started_at=now,
            content_id=content_id,
        )
        self._next_attempt_id += 1
        self.attempts[token] = attempt
        return attempt

    async def finish_sent(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        telegram_message_id: int,
    ) -> DailyDelivery | None:
        item = self._holder(lease_token)
        if item is None:
            self._lost(
                lease_token,
                at=at,
                outcome=DailyAttemptOutcome.SENT,
                error="Telegram accepted the message after the lease was taken over",
                telegram_message_id=telegram_message_id,
            )
            return None
        self._close(
            lease_token,
            at=at,
            outcome=DailyAttemptOutcome.SENT,
            telegram_message_id=telegram_message_id,
        )
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.SENT,
                sent_at=at,
                telegram_message_id=telegram_message_id,
                last_outcome=DailyAttemptOutcome.SENT,
                last_error=None,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )

    async def finish_retry(  # noqa: PLR0913 — mirrors the repository
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
        retry_after_seconds: int | None = None,
    ) -> DailyDelivery | None:
        item = self._holder(lease_token)
        if item is None:
            self._lost(lease_token, at=at, outcome=outcome, error=error)
            return None
        self._close(
            lease_token,
            at=at,
            outcome=outcome,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.PENDING,
                next_attempt_at=next_attempt_at,
                last_outcome=outcome,
                last_error=error,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )

    async def finish_failed(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
    ) -> DailyDelivery | None:
        item = self._holder(lease_token)
        if item is None:
            self._lost(lease_token, at=at, outcome=outcome, error=error)
            return None
        self._close(lease_token, at=at, outcome=outcome, error=error)
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.FAILED,
                last_outcome=outcome,
                last_error=error,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )

    async def finish_rejected(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
    ) -> DailyDelivery | None:
        item = self._holder(lease_token)
        if item is None:
            self._lost(lease_token, at=at, outcome=outcome, error=error)
            return None
        self._close(lease_token, at=at, outcome=outcome, error=error)
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.PENDING,
                content_id=None,
                next_attempt_at=next_attempt_at,
                last_outcome=outcome,
                last_error=error,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )

    async def mark_no_content(
        self,
        delivery_id: int,
        *,
        at: datetime,
        recheck_at: datetime,
    ) -> DailyDelivery:
        item = self.items.get(delivery_id)
        if item is None or item.status not in _DAILY_WAITING or item.content_id is not None:
            message = "Only an open delivery without an item can wait for content"
            raise ConflictError(message, delivery_id=delivery_id)
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.NO_CONTENT,
                next_attempt_at=recheck_at,
                updated_at=at,
            )
        )

    async def cancel(self, delivery_id: int, *, at: datetime) -> DailyDelivery | None:
        item = self.items.get(delivery_id)
        if item is None:
            return None
        dead_lease = (
            item.status is DailyDeliveryStatus.SENDING
            and item.lease_until is not None
            and item.lease_until <= at
        )
        if item.status not in _DAILY_WAITING and not dead_lease:
            return None
        if dead_lease:
            item = self._close_dangling(item, at=at)
        return self._store(
            replace(
                item,
                status=DailyDeliveryStatus.CANCELLED,
                lease_token=None,
                lease_until=None,
                updated_at=at,
            )
        )

    async def expire_overdue(self, *, now: datetime, limit: int = 100) -> int:
        overdue = sorted(
            (
                item
                for item in self.items.values()
                if item.deadline_at <= now and item.status in _DAILY_OPEN
            ),
            key=lambda item: item.deadline_at,
        )[:limit]
        for item in overdue:
            current = self._close_dangling(item, at=now) if item.lease_token else item
            self._store(
                replace(
                    current,
                    status=DailyDeliveryStatus.EXPIRED,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
        return len(overdue)

    async def attempts_of(self, delivery_id: int) -> tuple[DailyDeliveryAttempt, ...]:
        found = [item for item in self.attempts.values() if item.delivery_id == delivery_id]
        return tuple(sorted(found, key=lambda item: item.attempt_no))

    async def count_sent(self, user_id: int) -> int:
        return sum(
            1
            for item in self.items.values()
            if item.user_id == user_id and item.status is DailyDeliveryStatus.SENT
        )

    def _holder(self, lease_token: UUID) -> DailyDelivery | None:
        for item in self.items.values():
            if item.lease_token == lease_token and item.status is DailyDeliveryStatus.SENDING:
                return item
        return None

    def _close(  # noqa: PLR0913 — mirrors the repository
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str | None = None,
        retry_after_seconds: int | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        attempt = self.attempts[lease_token]
        self.attempts[lease_token] = replace(
            attempt,
            finished_at=at,
            outcome=outcome,
            error=error,
            retry_after_seconds=retry_after_seconds,
            telegram_message_id=telegram_message_id,
        )

    def _close_dangling(self, item: DailyDelivery, *, at: datetime) -> DailyDelivery:
        if item.lease_token is not None:
            attempt = self.attempts.get(item.lease_token)
            if attempt is not None and attempt.finished_at is None:
                self.attempts[item.lease_token] = replace(
                    attempt,
                    finished_at=at,
                    outcome=DailyAttemptOutcome.CRASHED,
                    error="The lease ran out before the attempt reported back",
                )
        return self._store(
            replace(
                item,
                last_outcome=DailyAttemptOutcome.CRASHED,
                last_error="The previous attempt never reported back",
            )
        )

    def _lost(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        telegram_message_id: int | None = None,
    ) -> None:
        attempt = self.attempts.get(lease_token)
        if attempt is None or attempt.outcome not in (None, DailyAttemptOutcome.CRASHED):
            return
        self.attempts[lease_token] = replace(
            attempt,
            finished_at=at,
            outcome=DailyAttemptOutcome.LEASE_LOST,
            error=f"{outcome.value}: {error}",
            telegram_message_id=telegram_message_id,
        )

    def _store(self, item: DailyDelivery) -> DailyDelivery:
        self.items[item.id] = item
        return item
