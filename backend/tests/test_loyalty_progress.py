"""Loyalty progress, read from real purchases (PostgreSQL), and never written.

What counts: a pack the buyer received — ``delivered``, or ``refunded`` after
delivery. What does not: pending, paid-but-undelivered, expired, and refunded
before delivery. Historical buyers — every one of them has no reward row yet —
see their rewards, and reading progress leaves every table untouched.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import LoyaltySettings
from app.core.exceptions import LoyaltyDisabledError
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.loyalty import MilestoneRewardState
from app.services.loyalty import LoyaltyService
from tests.fakes import FakeUnitOfWork, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory

BUYER = 9301
SOMEONE_ELSE = 9302
DELIVERY_URL = "https://t.me/+pack"

_PAID_FIRST = (PurchaseStatus.PAID, PurchaseStatus.DELIVERED, PurchaseStatus.REFUNDED)
"""States a purchase only reaches after being paid; the rest start pending."""

ON = LoyaltySettings(enabled=True)
OFF = LoyaltySettings(enabled=False)


async def _sale(
    uow: SqlAlchemyUnitOfWork,
    *,
    user_id: int = BUYER,
    status: PurchaseStatus,
    delivered_first: bool = False,
    provider: PaymentProvider = PaymentProvider.STARS,
) -> None:
    """One purchase of a fresh product, driven to ``status`` the way the shop does."""
    product = await uow.products.create(
        ProductDraft(
            slug=f"pack{uuid4().hex[:8]}",
            title="Pack",
            description="",
            delivery_url=DELIVERY_URL,
            price_stars=100,
        )
    )
    internal = provider is PaymentProvider.INTERNAL
    draft = PurchaseDraft(
        user_id=user_id,
        product_id=product.id,
        provider=provider,
        amount=Decimal(0) if internal else Decimal(100),
        base_amount=Decimal(100) if internal else None,
        loyalty_amount=Decimal(100) if internal else Decimal(0),
        currency=Currency.XTR,
        external_id=uuid4().hex,
        status=PurchaseStatus.PAID if internal or status in _PAID_FIRST else None,
    )
    purchase = await uow.purchases.create(draft)
    if status is PurchaseStatus.EXPIRED:
        await uow.purchases.expire(purchase.id)
    if status is PurchaseStatus.DELIVERED or delivered_first:
        await uow.purchases.mark_delivered(purchase.id, delivered_url=DELIVERY_URL)
    if status is PurchaseStatus.REFUNDED:
        await uow.purchases.mark_refunded(purchase.id)


async def _seed(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    sales: list[dict[str, Any]],
    *,
    user_id: int = BUYER,
) -> None:
    async with uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for sale in sales:
            await _sale(uow, user_id=user_id, **sale)


def _delivered(count: int) -> list[dict[str, Any]]:
    return [{"status": PurchaseStatus.DELIVERED}] * count


async def _snapshot(uow_factory: SqlAlchemyUnitOfWorkFactory) -> tuple[Any, ...]:
    """Everything a read could conceivably have changed, in one row."""
    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT"
                    " (SELECT count(*) FROM loyalty_rewards),"
                    " (SELECT count(*) FROM bonus_transactions),"
                    " (SELECT md5(coalesce(string_agg("
                    "   id::text || status::text || coalesce(delivered_at::text, ''),"
                    "   ',' ORDER BY id), '')) FROM purchases),"
                    " (SELECT md5(coalesce(string_agg("
                    "   telegram_id::text || last_seen_at::text, ',' ORDER BY telegram_id), ''))"
                    "  FROM users)"
                )
            )
        ).one()
    return tuple(row)


# --------------------------------------------------------------------------- #
# What counts                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sale", "counted"),
    [
        ({"status": PurchaseStatus.PENDING}, 0),
        ({"status": PurchaseStatus.PAID}, 0),
        ({"status": PurchaseStatus.DELIVERED}, 1),
        ({"status": PurchaseStatus.EXPIRED}, 0),
        ({"status": PurchaseStatus.REFUNDED, "delivered_first": True}, 1),
        ({"status": PurchaseStatus.REFUNDED}, 0),
        ({"status": PurchaseStatus.DELIVERED, "provider": PaymentProvider.CRYPTO}, 1),
        ({"status": PurchaseStatus.DELIVERED, "provider": PaymentProvider.INTERNAL}, 1),
        ({"status": PurchaseStatus.PAID, "provider": PaymentProvider.INTERNAL}, 0),
    ],
    ids=[
        "pending",
        "paid-undelivered",
        "delivered",
        "expired",
        "refunded-after-delivery",
        "refunded-before-delivery",
        "crypto-delivered",
        "internal-delivered",
        "internal-paid-undelivered",
    ],
)
async def test_each_purchase_state_counts_or_not(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    sale: dict[str, Any],
    counted: int,
) -> None:
    await _seed(live_uow_factory, [sale])

    async with live_uow_factory() as uow:
        assert await uow.loyalty.count_completed(BUYER) == counted


async def test_a_mixed_history_counts_only_what_was_received(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed(
        live_uow_factory,
        [
            {"status": PurchaseStatus.PENDING},
            {"status": PurchaseStatus.PENDING},
            {"status": PurchaseStatus.PAID},
            {"status": PurchaseStatus.DELIVERED},
            {"status": PurchaseStatus.DELIVERED, "provider": PaymentProvider.CRYPTO},
            {"status": PurchaseStatus.EXPIRED},
            {"status": PurchaseStatus.EXPIRED},
            {"status": PurchaseStatus.REFUNDED, "delivered_first": True},
            {"status": PurchaseStatus.REFUNDED},
            {"status": PurchaseStatus.DELIVERED, "provider": PaymentProvider.INTERNAL},
            {"status": PurchaseStatus.PAID, "provider": PaymentProvider.INTERNAL},
        ],
    )
    # Another buyer's packs never count towards this one.
    await _seed(live_uow_factory, _delivered(4), user_id=SOMEONE_ELSE)

    async with live_uow_factory() as uow:
        assert await uow.loyalty.count_completed(BUYER) == 4
        assert await uow.loyalty.count_completed(SOMEONE_ELSE) == 4
        assert await uow.loyalty.count_completed(424242) == 0
        assert await uow.loyalty.rewards_of(BUYER) == ()


# --------------------------------------------------------------------------- #
# Historical buyers, through the service                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("completed", "discount", "free", "vip"),
    [
        (0, False, False, False),
        (4, False, False, False),
        (5, True, False, False),
        (10, True, True, False),
        (13, True, True, False),
        (15, True, True, True),
        (18, True, True, True),
    ],
)
async def test_a_historical_buyer_sees_every_reward_they_earned(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    completed: int,
    discount: bool,
    free: bool,
    vip: bool,
) -> None:
    """Their purchases predate the programme; nothing was ever recorded for them."""
    await _seed(live_uow_factory, _delivered(completed))
    before = await _snapshot(live_uow_factory)
    service = LoyaltyService(uow_factory=live_uow_factory, settings=ON)

    progress = await service.get_progress(BUYER)

    assert progress.completed_count == completed
    assert progress.discount_50_available is discount
    assert progress.free_pack_available is free
    assert progress.vip is vip
    assert progress.to_free == max(10 - completed, 0)
    assert progress.to_vip == max(15 - completed, 0)
    # Reading recorded nothing: no reward row, no change anywhere.
    assert await _snapshot(live_uow_factory) == before
    assert before[0] == 0


async def test_reading_progress_again_and_again_changes_nothing(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed(live_uow_factory, _delivered(12))
    service = LoyaltyService(uow_factory=live_uow_factory, settings=ON)
    before = await _snapshot(live_uow_factory)

    results = [await service.get_progress(BUYER) for _ in range(5)]

    assert len(set(results)) == 1
    assert await _snapshot(live_uow_factory) == before


async def test_concurrent_reads_neither_create_nor_change_anything(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed(live_uow_factory, _delivered(16))
    service = LoyaltyService(uow_factory=live_uow_factory, settings=ON)
    before = await _snapshot(live_uow_factory)

    results = await asyncio.gather(*(service.get_progress(BUYER) for _ in range(20)))

    assert {result.completed_count for result in results} == {16}
    assert all(result.discount_50 is MilestoneRewardState.AVAILABLE for result in results)
    assert all(result.free_pack is MilestoneRewardState.AVAILABLE for result in results)
    assert all(result.vip for result in results)
    assert await _snapshot(live_uow_factory) == before


async def test_recorded_rewards_are_read_back(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Rows written by a later step decide their own state; the count does the rest."""
    await _seed(live_uow_factory, _delivered(11))
    async with live_uow_factory() as uow:
        await uow.session.execute(
            text(
                "INSERT INTO loyalty_rewards (id, user_id, milestone, kind, status,"
                " consumed_purchase_id, consumed_at)"
                " SELECT gen_random_uuid(), :user_id, 5, 'discount_50_percent', 'consumed',"
                " id, now() FROM purchases WHERE user_id = :user_id LIMIT 1"
            ),
            {"user_id": BUYER},
        )
    service = LoyaltyService(uow_factory=live_uow_factory, settings=ON)

    progress = await service.get_progress(BUYER)

    assert progress.discount_50 is MilestoneRewardState.USED
    assert progress.free_pack is MilestoneRewardState.AVAILABLE
    async with live_uow_factory() as uow:
        rewards = await uow.loyalty.rewards_of(BUYER)
    assert [(reward.milestone, reward.status.value) for reward in rewards] == [(5, "consumed")]


# --------------------------------------------------------------------------- #
# The switch                                                                  #
# --------------------------------------------------------------------------- #


async def test_progress_cannot_be_read_while_the_programme_is_off() -> None:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    service = LoyaltyService(uow_factory=factory, settings=OFF)

    assert not service.enabled
    with pytest.raises(LoyaltyDisabledError):
        await service.get_progress(BUYER)
    # Refused before anything was even read.
    assert factory.scopes == 0


async def test_the_fake_read_model_counts_exactly_like_the_sql() -> None:
    """The in-memory double used by the bot tests keeps the same rule."""
    unit = FakeUnitOfWork()
    service = LoyaltyService(uow_factory=FakeUnitOfWorkFactory(unit), settings=ON)
    await unit.users.upsert(UserDraft(telegram_id=BUYER))
    product = await unit.products.create(
        ProductDraft(
            slug="fakepack",
            title="Pack",
            description="",
            delivery_url=DELIVERY_URL,
            price_stars=100,
        )
    )
    for status in (PurchaseStatus.PAID, PurchaseStatus.PAID, PurchaseStatus.PAID):
        paid = await unit.purchases.create(
            PurchaseDraft(
                user_id=BUYER,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(100),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=status,
            )
        )
        await unit.purchases.mark_delivered(paid.id, delivered_url=DELIVERY_URL)
    delivered = next(iter(unit.purchases.items))
    await unit.purchases.mark_refunded(delivered)

    progress = await service.get_progress(BUYER)

    assert progress.completed_count == 3
