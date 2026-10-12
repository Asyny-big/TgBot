"""The database's own rules for internal grants and loyalty rewards (real PostgreSQL).

Migration 0005 adds them; nothing in the application writes such rows yet. These
tests prove the schema already refuses every shape the loyalty programme must
never produce, and that an internal grant stays out of money and referral
numbers while still being a pack the buyer received.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.domain.commands import PurchaseDraft, ReferralDraft
from app.domain.enums import (
    Currency,
    LoyaltyRewardKind,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import DISCOUNT_MILESTONE
from app.domain.pagination import PageRequest, ReferralFilters
from app.domain.stats import StatsPeriod
from app.infrastructure.db.mappers import to_loyalty_reward
from app.infrastructure.db.models import LoyaltyRewardModel, PurchaseModel
from tests.db import product_draft, seed_paid_purchase, user_draft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import Product, Purchase, User
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.referrals import SqlAlchemyReferralRepository
    from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

INVITER = 8201
BUYER = 8202
DELIVERY_URL = "https://t.me/+pack"


@pytest.fixture
async def buyer(users: SqlAlchemyUserRepository) -> User:
    return await users.upsert(user_draft(BUYER))


@pytest.fixture
async def product(products: SqlAlchemyProductRepository) -> Product:
    return await products.create(product_draft(price_stars=100, price_usdt=Decimal("5.00")))


def _grant(buyer: User, product: Product, **overrides: Any) -> PurchaseDraft:
    """A free pack shaped exactly as the schema requires, unless overridden."""
    values: dict[str, Any] = {
        "user_id": buyer.telegram_id,
        "product_id": product.id,
        "provider": PaymentProvider.INTERNAL,
        "amount": Decimal(0),
        "base_amount": Decimal(100),
        "loyalty_amount": Decimal(100),
        "currency": Currency.XTR,
        "external_id": f"loyalty:{uuid4()}",
        "status": PurchaseStatus.PAID,
    }
    values.update(overrides)
    return PurchaseDraft(**values)


async def _refused(
    purchases: SqlAlchemyPurchaseRepository,
    draft: PurchaseDraft,
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError) as caught:
        await purchases.create(draft)
    assert constraint in str(caught.value.orig)


# --------------------------------------------------------------------------- #
# Purchases, amount and provider                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "currency"),
    [(PaymentProvider.STARS, Currency.XTR), (PaymentProvider.CRYPTO, Currency.USDT)],
)
async def test_a_payment_rail_can_never_record_a_zero_amount(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
    provider: PaymentProvider,
    currency: Currency,
) -> None:
    await _refused(
        purchases,
        PurchaseDraft(
            user_id=buyer.telegram_id,
            product_id=product.id,
            provider=provider,
            amount=Decimal(0),
            currency=currency,
            external_id=uuid4().hex,
        ),
        "ck_purchases_amount_matches_provider",
    )


async def test_an_internal_grant_can_never_bill_anything(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    await _refused(
        purchases,
        _grant(buyer, product, amount=Decimal(1), loyalty_amount=Decimal(99)),
        "ck_purchases_amount_matches_provider",
    )


async def test_an_internal_grant_can_never_be_pending(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    """Never pending, so reconciliation and expiry never see one."""
    await _refused(
        purchases,
        _grant(buyer, product, status=PurchaseStatus.PENDING),
        "ck_purchases_internal_is_loyalty_grant",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"loyalty_amount": Decimal(50)},
        {"base_amount": None},
        {"base_amount": Decimal(0), "loyalty_amount": Decimal(0)},
    ],
    ids=["partial-waiver", "no-list-price", "zero-list-price"],
)
async def test_an_internal_grant_waives_exactly_its_whole_list_price(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(IntegrityError):
        await purchases.create(_grant(buyer, product, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"discount_amount": Decimal(10), "loyalty_amount": Decimal(90)},
        {"bonus_amount": Decimal(10), "loyalty_amount": Decimal(90)},
    ],
    ids=["with-referral-discount", "with-bonuses"],
)
async def test_an_internal_grant_carries_no_other_reduction(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
    overrides: dict[str, Any],
) -> None:
    await _refused(
        purchases,
        _grant(buyer, product, **overrides),
        "ck_purchases_internal_is_loyalty_grant",
    )


async def test_the_breakdown_must_now_account_for_the_loyalty_amount(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    """100 - 50 loyalty must bill 50: billing 100 would leave the waiver unexplained."""
    await _refused(
        purchases,
        PurchaseDraft(
            user_id=buyer.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(100),
            base_amount=Decimal(100),
            loyalty_amount=Decimal(50),
            currency=Currency.XTR,
            external_id=uuid4().hex,
        ),
        "ck_purchases_amount_breakdown_consistent",
    )
    half = await purchases.create(
        PurchaseDraft(
            user_id=buyer.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(50),
            base_amount=Decimal(100),
            loyalty_amount=Decimal(50),
            currency=Currency.XTR,
            external_id=uuid4().hex,
        )
    )
    assert half.loyalty_amount == Decimal(50)


async def test_an_existing_paid_purchase_is_unaffected(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    """Rows written before 0005 read back with a zero loyalty amount."""
    paid = await seed_paid_purchase(purchases, buyer, product)

    assert paid.loyalty_amount == Decimal(0)
    assert paid.amount == Decimal(100)


async def test_a_delivered_free_pack_is_a_completed_purchase(
    purchases: SqlAlchemyPurchaseRepository,
    db_session: AsyncSession,
    buyer: User,
    product: Product,
) -> None:
    """Delivered like any pack, so the progress count the programme will use sees it."""
    grant = await purchases.create(_grant(buyer, product))
    assert grant.status is PurchaseStatus.PAID
    assert grant.paid_at is not None

    delivered = await purchases.mark_delivered(grant.id, delivered_url=DELIVERY_URL)

    assert delivered.status is PurchaseStatus.DELIVERED
    assert delivered.delivered_at is not None
    assert delivered.amount == Decimal(0)
    assert delivered.loyalty_amount == delivered.base_amount == Decimal(100)
    completed = await db_session.scalar(
        select(func.count()).where(
            PurchaseModel.user_id == buyer.telegram_id,
            or_(
                PurchaseModel.status == PurchaseStatus.DELIVERED,
                (PurchaseModel.status == PurchaseStatus.REFUNDED)
                & PurchaseModel.delivered_at.is_not(None),
            ),
        )
    )
    assert completed == 1


# --------------------------------------------------------------------------- #
# Money and referral numbers                                                  #
# --------------------------------------------------------------------------- #


async def test_a_free_pack_is_neither_revenue_nor_a_paid_purchase(
    purchases: SqlAlchemyPurchaseRepository,
    products: SqlAlchemyProductRepository,
    stats: SqlAlchemyStatsRepository,
    buyer: User,
    product: Product,
) -> None:
    sold = await products.create(product_draft(price_stars=150, price_usdt=None))
    await seed_paid_purchase(purchases, buyer, sold)
    grant = await purchases.create(_grant(buyer, product))
    await purchases.mark_delivered(grant.id, delivered_url=DELIVERY_URL)

    summary = await stats.revenue(StatsPeriod.ALL)
    top = await stats.top_products(StatsPeriod.ALL)
    recent = await stats.recent_purchases()

    assert summary.purchases_count == 1
    assert summary.stars_amount == 150
    assert summary.usdt_amount == Decimal(0)
    assert [entry.product_id for entry in top] == [sold.id]
    # Still listed as something that was handed over.
    assert grant.id in {record.purchase.id for record in recent}


async def test_a_free_pack_is_not_a_referral_purchase(
    purchases: SqlAlchemyPurchaseRepository,
    products: SqlAlchemyProductRepository,
    referrals: SqlAlchemyReferralRepository,
    users: SqlAlchemyUserRepository,
    buyer: User,
) -> None:
    await users.upsert(user_draft(INVITER))
    await referrals.create(ReferralDraft(referrer_user_id=INVITER, referred_user_id=BUYER))
    sold = await products.create(product_draft(price_stars=150, price_usdt=None))
    free = await products.create(product_draft(price_stars=100, price_usdt=None))
    await seed_paid_purchase(purchases, buyer, sold)
    grant = await purchases.create(_grant(buyer, free))
    await purchases.mark_delivered(grant.id, delivered_url=DELIVERY_URL)

    assert await referrals.count_referral_purchases(INVITER) == 1
    page = await referrals.search(ReferralFilters(), PageRequest())
    assert [record.referred_purchase_count for record in page.items] == [1]


# --------------------------------------------------------------------------- #
# loyalty_rewards                                                             #
# --------------------------------------------------------------------------- #


async def _insert_reward(session: AsyncSession, **values: Any) -> None:
    """Insert one reward inside a savepoint, so a refused one leaves the session usable."""
    async with session.begin_nested():
        session.add(LoyaltyRewardModel(id=uuid4(), **values))
        await session.flush()


async def _refused_reward(session: AsyncSession, constraint: str, **values: Any) -> None:
    with pytest.raises(IntegrityError) as caught:
        await _insert_reward(session, **values)
    assert constraint in str(caught.value.orig)


def _discount(buyer: User, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": buyer.telegram_id,
        "milestone": 5,
        "kind": LoyaltyRewardKind.DISCOUNT_50_PERCENT,
    }
    values.update(overrides)
    return values


async def test_a_milestone_rewards_a_buyer_once(db_session: AsyncSession, buyer: User) -> None:
    await _insert_reward(db_session, **_discount(buyer))
    await _refused_reward(db_session, "uq_loyalty_rewards_user_milestone", **_discount(buyer))


async def test_a_new_reward_starts_available(db_session: AsyncSession, buyer: User) -> None:
    await _insert_reward(db_session, **_discount(buyer))
    model = await db_session.scalar(
        select(LoyaltyRewardModel).where(LoyaltyRewardModel.user_id == buyer.telegram_id)
    )
    assert model is not None

    reward = to_loyalty_reward(model)

    assert reward.status is LoyaltyRewardStatus.AVAILABLE
    assert reward.kind is LoyaltyRewardKind.DISCOUNT_50_PERCENT
    assert reward.milestone == DISCOUNT_MILESTONE
    assert reward.earned_at is not None
    assert reward.applied_purchase_id is None
    assert reward.consumed_purchase_id is None


@pytest.mark.parametrize(
    ("milestone", "kind"),
    [
        (5, LoyaltyRewardKind.FREE_PACK),
        (10, LoyaltyRewardKind.DISCOUNT_50_PERCENT),
        (15, LoyaltyRewardKind.FREE_PACK),
    ],
    ids=["5-free", "10-discount", "vip-is-not-a-reward"],
)
async def test_each_milestone_grants_only_its_own_reward(
    db_session: AsyncSession,
    buyer: User,
    milestone: int,
    kind: LoyaltyRewardKind,
) -> None:
    await _refused_reward(
        db_session,
        "ck_loyalty_rewards_milestone_kind",
        user_id=buyer.telegram_id,
        milestone=milestone,
        kind=kind,
    )


async def _purchase_id(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> Purchase:
    return await purchases.create(
        PurchaseDraft(
            user_id=buyer.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(50),
            base_amount=Decimal(100),
            loyalty_amount=Decimal(50),
            currency=Currency.XTR,
            external_id=uuid4().hex,
        )
    )


@pytest.mark.parametrize(
    "shape",
    [
        "available-with-purchase",
        "applied-without-purchase",
        "applied-without-time",
        "consumed-without-purchase",
        "consumed-without-time",
    ],
)
async def test_every_status_carries_exactly_its_own_facts(
    db_session: AsyncSession,
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
    shape: str,
) -> None:
    purchase = await _purchase_id(purchases, buyer, product)
    now = datetime.now(UTC)
    shapes: dict[str, dict[str, Any]] = {
        "available-with-purchase": {"applied_purchase_id": purchase.id, "applied_at": now},
        "applied-without-purchase": {"status": LoyaltyRewardStatus.APPLIED, "applied_at": now},
        "applied-without-time": {
            "status": LoyaltyRewardStatus.APPLIED,
            "applied_purchase_id": purchase.id,
        },
        "consumed-without-purchase": {
            "status": LoyaltyRewardStatus.CONSUMED,
            "consumed_at": now,
        },
        "consumed-without-time": {
            "status": LoyaltyRewardStatus.CONSUMED,
            "consumed_purchase_id": purchase.id,
        },
    }
    await _refused_reward(
        db_session,
        "ck_loyalty_rewards_state_shape",
        **_discount(buyer, **shapes[shape]),
    )


async def test_the_three_valid_states_are_accepted(
    db_session: AsyncSession,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
    buyer: User,
    product: Product,
) -> None:
    applied_to = await _purchase_id(purchases, buyer, product)
    other = await users.upsert(user_draft(BUYER + 1))
    consumed_by = await purchases.create(_grant(other, product))
    now = datetime.now(UTC)

    await _insert_reward(
        db_session,
        **_discount(
            buyer,
            status=LoyaltyRewardStatus.APPLIED,
            applied_purchase_id=applied_to.id,
            applied_at=now,
        ),
    )
    await _insert_reward(
        db_session,
        user_id=other.telegram_id,
        milestone=10,
        kind=LoyaltyRewardKind.FREE_PACK,
        status=LoyaltyRewardStatus.CONSUMED,
        consumed_purchase_id=consumed_by.id,
        consumed_at=now,
    )


async def test_a_free_pack_is_never_applied_to_an_invoice(
    db_session: AsyncSession,
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    purchase = await _purchase_id(purchases, buyer, product)
    await _refused_reward(
        db_session,
        "ck_loyalty_rewards_free_pack_never_applied",
        user_id=buyer.telegram_id,
        milestone=10,
        kind=LoyaltyRewardKind.FREE_PACK,
        status=LoyaltyRewardStatus.APPLIED,
        applied_purchase_id=purchase.id,
        applied_at=datetime.now(UTC),
    )


async def test_one_purchase_carries_at_most_one_applied_reward(
    db_session: AsyncSession,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
    buyer: User,
    product: Product,
) -> None:
    purchase = await _purchase_id(purchases, buyer, product)
    other = await users.upsert(user_draft(BUYER + 1))
    applied = {
        "status": LoyaltyRewardStatus.APPLIED,
        "applied_purchase_id": purchase.id,
        "applied_at": datetime.now(UTC),
    }
    await _insert_reward(db_session, **_discount(buyer, **applied))
    await _refused_reward(
        db_session,
        "uq_loyalty_rewards_applied_purchase_id",
        **_discount(other, **applied),
    )


async def test_one_purchase_consumes_at_most_one_reward(
    db_session: AsyncSession,
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    grant = await purchases.create(_grant(buyer, product))
    consumed = {
        "status": LoyaltyRewardStatus.CONSUMED,
        "consumed_purchase_id": grant.id,
        "consumed_at": datetime.now(UTC),
    }
    await _insert_reward(
        db_session,
        user_id=buyer.telegram_id,
        milestone=10,
        kind=LoyaltyRewardKind.FREE_PACK,
        **consumed,
    )
    await _refused_reward(
        db_session,
        "uq_loyalty_rewards_consumed_purchase_id",
        **_discount(buyer, **consumed),
    )
