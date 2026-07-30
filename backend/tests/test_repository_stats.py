"""Integration tests for the statistics repository and the user repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.domain.enums import PaymentProvider, PurchaseStatus
from app.domain.stats import StatsPeriod
from tests.db import product_draft, purchase_draft, seed_paid_purchase, user_draft

if TYPE_CHECKING:
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

NOW = datetime.now(UTC)


async def test_user_upsert_inserts_then_refreshes(users: SqlAlchemyUserRepository) -> None:
    first_seen = NOW - timedelta(days=3)
    created = await users.upsert(user_draft(42, username="old_name"), seen_at=first_seen)
    assert created.telegram_id == 42
    assert created.username == "old_name"
    assert created.last_seen_at == first_seen

    updated = await users.upsert(user_draft(42, username="new_name"), seen_at=NOW)
    assert updated.username == "new_name"
    assert updated.last_seen_at == NOW
    assert updated.created_at == created.created_at
    assert await users.count() == 1
    assert await users.get(42) == updated
    assert await users.get(43) is None


async def test_revenue_splits_currencies_and_ignores_unpaid(
    stats: SqlAlchemyStatsRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
) -> None:
    stars_product = await products.create(product_draft(slug="stars-only", price_stars=150))
    crypto_product = await products.create(
        product_draft(slug="crypto-only", price_usdt=Decimal("9.50"))
    )
    first = await users.upsert(user_draft(1))
    second = await users.upsert(user_draft(2))

    await seed_paid_purchase(purchases, first, stars_product)
    await seed_paid_purchase(purchases, second, crypto_product, PaymentProvider.CRYPTO)
    # Pending and refunded purchases must not be counted as revenue.
    await purchases.create(purchase_draft(first, crypto_product, PaymentProvider.CRYPTO))
    refunded = await seed_paid_purchase(purchases, second, stars_product)
    await purchases.mark_refunded(refunded.id)

    summary = await stats.revenue(StatsPeriod.ALL)
    assert summary.purchases_count == 2
    assert summary.stars_amount == 150
    assert summary.usdt_amount == Decimal("9.50")


async def test_revenue_windows_respect_paid_at(
    stats: SqlAlchemyStatsRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
) -> None:
    product = await products.create(product_draft(price_stars=10))
    today_buyer = await users.upsert(user_draft(10))
    week_buyer = await users.upsert(user_draft(11))
    old_buyer = await users.upsert(user_draft(12))

    fresh = await purchases.create(purchase_draft(today_buyer, product, external_id="inv-today"))
    await purchases.mark_paid(fresh.id, paid_at=NOW)

    mid = await purchases.create(purchase_draft(week_buyer, product, external_id="inv-week"))
    await purchases.mark_paid(mid.id, paid_at=NOW - timedelta(days=3))

    old = await purchases.create(purchase_draft(old_buyer, product, external_id="inv-old"))
    await purchases.mark_paid(old.id, paid_at=NOW - timedelta(days=60))

    assert (await stats.revenue(StatsPeriod.TODAY, now=NOW)).purchases_count == 1
    assert (await stats.revenue(StatsPeriod.WEEK, now=NOW)).purchases_count == 2
    assert (await stats.revenue(StatsPeriod.MONTH, now=NOW)).purchases_count == 2
    assert (await stats.revenue(StatsPeriod.ALL, now=NOW)).purchases_count == 3


async def test_revenue_of_an_empty_shop_is_zero(stats: SqlAlchemyStatsRepository) -> None:
    summary = await stats.revenue(StatsPeriod.ALL)
    assert summary.purchases_count == 0
    assert summary.stars_amount == 0
    assert summary.usdt_amount == Decimal("0")


async def test_top_products_are_ordered_by_sales(
    stats: SqlAlchemyStatsRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
) -> None:
    popular = await products.create(product_draft(slug="popular", title="Popular", price_stars=50))
    niche = await products.create(product_draft(slug="niche", title="Niche", price_stars=70))

    for telegram_id in (101, 102, 103):
        buyer = await users.upsert(user_draft(telegram_id))
        await seed_paid_purchase(purchases, buyer, popular)
    single = await users.upsert(user_draft(104))
    await seed_paid_purchase(purchases, single, niche)

    top = await stats.top_products(StatsPeriod.ALL)
    assert [item.slug for item in top] == ["popular", "niche"]
    assert top[0].purchases_count == 3
    assert top[0].stars_amount == 150
    assert top[1].stars_amount == 70

    assert len(await stats.top_products(StatsPeriod.ALL, limit=1)) == 1


async def test_recent_purchases_are_newest_first(
    stats: SqlAlchemyStatsRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    users: SqlAlchemyUserRepository,
) -> None:
    product = await products.create(product_draft(price_stars=10))
    older_buyer = await users.upsert(user_draft(201))
    newer_buyer = await users.upsert(user_draft(202))

    older = await purchases.create(purchase_draft(older_buyer, product, external_id="inv-older"))
    await purchases.mark_paid(older.id, paid_at=NOW - timedelta(hours=2))
    newer = await purchases.create(purchase_draft(newer_buyer, product, external_id="inv-newer"))
    await purchases.mark_paid(newer.id, paid_at=NOW)
    # Pending purchases never appear in the dashboard feed.
    pending_buyer = await users.upsert(user_draft(203))
    await purchases.create(purchase_draft(pending_buyer, product, external_id="inv-pending"))

    recent = await stats.recent_purchases(limit=5)
    assert [record.purchase.id for record in recent] == [newer.id, older.id]
    assert recent[0].user.telegram_id == 202
    assert recent[0].product.slug == product.slug
    assert all(record.purchase.status is PurchaseStatus.PAID for record in recent)
