"""Stage 2 defects a-c: the bonus ledger's edge cases and its housekeeping.

a. A bonus worth less than a cent was reserved and spent for a 0.00 discount.
b. ``stale_holds`` kept returning reservations it had already released, so once
   a batch filled up with them, newly dead reservations were never released.
c. ``purchases_awaiting_reward`` kept returning delivered purchases that earn a
   reward of zero, so once a batch filled up with them, purchases that really
   missed a reward were never repaired.

The arithmetic of (a) is covered in ``test_domain_bonuses``; here it is shown
end to end through checkout. (b) and (c) are about SQL selection criteria, so
they run against PostgreSQL — the in-memory fakes mirror the rule, but only the
real query can prove it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.config import ReferralSettings
from app.core.exceptions import InsufficientBonusBalanceError
from app.domain.bonuses import BonusPolicy, reward_bonuses
from app.domain.commands import (
    BonusTransactionDraft,
    ProductDraft,
    PurchaseDraft,
    ReferralDraft,
    UserDraft,
)
from app.domain.enums import BonusTransactionType, Currency, PaymentProvider, PurchaseStatus
from app.services.bonuses import BonusService
from app.services.purchases import PurchaseService
from tests.fakes import FakeLockManager, FakeUnitOfWork, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import Product, Purchase
    from app.domain.uow import UnitOfWork
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

INVITER = 9201
INVITED = 9202
BATCH = 50
"""The housekeeping batch size the bot runs with (BOT_RECONCILIATION_BATCH_SIZE)."""

REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)
DELIVERY_URL = "https://t.me/+pack"


def _product_draft(
    *,
    price_stars: int | None = 1000,
    price_usdt: Decimal | None = Decimal("10.00"),
) -> ProductDraft:
    return ProductDraft(
        slug=f"pack{uuid4().hex[:8]}",
        title="Pack",
        description="",
        delivery_url=DELIVERY_URL,
        price_stars=price_stars,
        price_usdt=price_usdt,
    )


async def _grant(uow: UnitOfWork | FakeUnitOfWork, user_id: int, units: int) -> None:
    await uow.bonuses.add(
        BonusTransactionDraft(
            user_id=user_id,
            amount=units,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )


# --------------------------------------------------------------------------- #
# (a) A bonus worth less than a cent, end to end                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def unit() -> FakeUnitOfWork:
    return FakeUnitOfWork()


async def _cheap_star_shop(
    unit: FakeUnitOfWork, *, balance: int
) -> tuple[PurchaseService, Product]:
    """1000 ⭐ or 5.00 USDT: 200 Stars per USDT, so one bonus is worth 0.005."""
    await unit.users.upsert(UserDraft(telegram_id=INVITED))
    await _grant(unit, INVITED, balance)
    product = await unit.products.create(_product_draft(price_usdt=Decimal("5.00")))
    factory = FakeUnitOfWorkFactory(unit)
    locks = FakeLockManager()
    pricing = BonusService(uow_factory=factory, locks=locks, settings=REFERRAL)
    return PurchaseService(uow_factory=factory, locks=locks, pricing=pricing), product


def _reserves(unit: FakeUnitOfWork) -> list[BonusTransactionType]:
    return [entry.type for entry in unit.bonuses.entries if entry.purchase_id is not None]


async def test_a_bonus_worth_nothing_is_never_offered(unit: FakeUnitOfWork) -> None:
    purchases, product = await _cheap_star_shop(unit, balance=1)
    pricing = purchases.pricing
    assert isinstance(pricing, BonusService)

    offer = await pricing.offer_for(
        user_id=INVITED,
        base_amount=Decimal("5.00"),
        currency=Currency.USDT,
        stars_per_usdt=product.stars_per_usdt,
    )

    assert not offer.available
    assert offer.balance == 1


async def test_a_bonus_worth_nothing_is_neither_reserved_nor_spent(unit: FakeUnitOfWork) -> None:
    """Asking for it anyway (a hand-crafted callback) is refused, and nothing moves."""
    purchases, product = await _cheap_star_shop(unit, balance=1)

    with pytest.raises(InsufficientBonusBalanceError):
        await purchases.start_purchase(
            user_id=INVITED,
            product_id=product.id,
            provider=PaymentProvider.CRYPTO,
            external_id="9001",
            use_bonus=True,
        )

    assert _reserves(unit) == []
    assert unit.users.items[INVITED].bonus_balance == 1
    assert unit.purchases.items == {}


async def test_without_bonuses_the_full_price_is_charged_and_the_bonus_kept(
    unit: FakeUnitOfWork,
) -> None:
    purchases, product = await _cheap_star_shop(unit, balance=1)

    purchase = await purchases.start_purchase(
        user_id=INVITED,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="9002",
    )

    assert purchase.amount == Decimal("5.00")
    assert purchase.bonus_amount == 0
    assert _reserves(unit) == []
    assert unit.users.items[INVITED].bonus_balance == 1


async def test_bonuses_that_do_buy_a_cent_are_reserved_and_only_those(
    unit: FakeUnitOfWork,
) -> None:
    """Three bonuses at 200 ⭐/USDT buy one cent; two of them are enough."""
    purchases, product = await _cheap_star_shop(unit, balance=3)

    purchase = await purchases.start_purchase(
        user_id=INVITED,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="9003",
        use_bonus=True,
    )

    assert purchase.bonus_amount == Decimal("0.01")
    assert purchase.amount == Decimal("4.99")
    reserve = [e for e in unit.bonuses.entries if e.type is BonusTransactionType.BONUS_RESERVED]
    assert [entry.amount for entry in reserve] == [-2]
    assert unit.users.items[INVITED].bonus_balance == 1


async def test_a_stars_purchase_still_spends_a_single_bonus(unit: FakeUnitOfWork) -> None:
    purchases, product = await _cheap_star_shop(unit, balance=1)

    purchase = await purchases.start_purchase(
        user_id=INVITED,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-1",
        use_bonus=True,
    )

    assert purchase.bonus_amount == Decimal(1)
    assert purchase.amount == Decimal(999)
    assert unit.users.items[INVITED].bonus_balance == 0


# --------------------------------------------------------------------------- #
# Live fixtures for (b) and (c)                                               #
# --------------------------------------------------------------------------- #


def _bonus_service(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    locks: RedisLockManager,
) -> BonusService:
    return BonusService(uow_factory=uow_factory, locks=locks, settings=REFERRAL)


async def _seed_people(uow_factory: SqlAlchemyUnitOfWorkFactory, *, balance: int = 0) -> None:
    async with uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=INVITER))
        await uow.users.upsert(UserDraft(telegram_id=INVITED))
        await uow.referrals.create(
            ReferralDraft(referrer_user_id=INVITER, referred_user_id=INVITED)
        )
        if balance:
            await _grant(uow, INVITED, balance)


async def _dead_reservation(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    product: Product,
    *,
    units: int = 1,
) -> Purchase:
    """A pending invoice holding ``units`` bonuses, then expired unpaid."""
    async with uow_factory() as uow:
        purchase = await uow.purchases.create(
            PurchaseDraft(
                user_id=INVITED,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(product.price_stars or 0) - units,
                base_amount=Decimal(product.price_stars or 0),
                bonus_amount=Decimal(units),
                currency=Currency.XTR,
                external_id=uuid4().hex,
            )
        )
        await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=INVITED,
                amount=-units,
                type=BonusTransactionType.BONUS_RESERVED,
                purchase_id=purchase.id,
            )
        )
        return await uow.purchases.expire(purchase.id)


# --------------------------------------------------------------------------- #
# (b) Stale holds                                                             #
# --------------------------------------------------------------------------- #


async def test_a_released_hold_is_never_selected_again(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    await _seed_people(live_uow_factory, balance=10)
    async with live_uow_factory() as uow:
        product = await uow.products.create(_product_draft())
    dead = await _dead_reservation(live_uow_factory, product, units=4)
    service = _bonus_service(live_uow_factory, live_locks)

    async with live_uow_factory() as uow:
        assert await uow.bonuses.stale_holds() == (dead.id,)
    assert await service.release_stale_holds() == 1

    # Every later pass finds nothing — not "finds it and skips it".
    for _ in range(3):
        async with live_uow_factory() as uow:
            assert await uow.bonuses.stale_holds() == ()
        assert await service.release_stale_holds() == 0

    async with live_uow_factory() as uow:
        released = await uow.bonuses.find_for_purchase(
            dead.id, BonusTransactionType.BONUS_RESERVE_RELEASED
        )
        assert released is not None
        assert released.amount == 4
        assert await uow.bonuses.balance(INVITED) == 10
        assert await uow.bonuses.recompute_balance(INVITED) == 10


async def test_a_new_dead_hold_is_found_behind_a_batch_of_released_ones(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """The production failure: more released holds than the batch size.

    Before the fix the oldest fifty released holds were selected on every pass,
    so the 51st hold onward — and anything that died later — was never reached.
    """
    released_before = BATCH + 5
    await _seed_people(live_uow_factory, balance=released_before + 1)
    async with live_uow_factory() as uow:
        product = await uow.products.create(_product_draft())
    for _ in range(released_before):
        await _dead_reservation(live_uow_factory, product)
    service = _bonus_service(live_uow_factory, live_locks)

    assert await service.release_stale_holds(limit=BATCH) == BATCH
    assert await service.release_stale_holds(limit=BATCH) == released_before - BATCH
    assert await service.release_stale_holds(limit=BATCH) == 0

    newest = await _dead_reservation(live_uow_factory, product)
    async with live_uow_factory() as uow:
        assert await uow.bonuses.stale_holds(limit=BATCH) == (newest.id,)
    assert await service.release_stale_holds(limit=BATCH) == 1

    async with live_uow_factory() as uow:
        assert await uow.bonuses.balance(INVITED) == released_before + 1
        assert await uow.bonuses.recompute_balance(INVITED) == released_before + 1
        assert await uow.bonuses.stale_holds(limit=BATCH) == ()


async def test_a_spent_hold_is_never_stale_even_after_a_refund(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Settling turns the row into ``bonus_spent``; a refund gives nothing back."""
    await _seed_people(live_uow_factory, balance=10)
    async with live_uow_factory() as uow:
        product = await uow.products.create(_product_draft())
        purchase = await uow.purchases.create(
            PurchaseDraft(
                user_id=INVITED,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(995),
                base_amount=Decimal(1000),
                bonus_amount=Decimal(5),
                currency=Currency.XTR,
                external_id=uuid4().hex,
            )
        )
        await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=INVITED,
                amount=-5,
                type=BonusTransactionType.BONUS_RESERVED,
                purchase_id=purchase.id,
            )
        )
        await uow.purchases.mark_paid(purchase.id)
        await uow.bonuses.settle_hold(purchase.id)
        await uow.purchases.mark_refunded(purchase.id)

    async with live_uow_factory() as uow:
        assert await uow.bonuses.stale_holds() == ()
        assert await uow.bonuses.balance(INVITED) == 5


async def test_a_hold_whose_invoice_is_still_payable_is_not_stale(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed_people(live_uow_factory, balance=10)
    async with live_uow_factory() as uow:
        product = await uow.products.create(_product_draft())
        purchase = await uow.purchases.create(
            PurchaseDraft(
                user_id=INVITED,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(998),
                base_amount=Decimal(1000),
                bonus_amount=Decimal(2),
                currency=Currency.XTR,
                external_id=uuid4().hex,
            )
        )
        await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=INVITED,
                amount=-2,
                type=BonusTransactionType.BONUS_RESERVED,
                purchase_id=purchase.id,
            )
        )

    async with live_uow_factory() as uow:
        assert await uow.bonuses.stale_holds() == ()


# --------------------------------------------------------------------------- #
# (c) Purchases awaiting a reward                                             #
# --------------------------------------------------------------------------- #


async def _delivered(
    uow: UnitOfWork,
    product: Product,
    *,
    amount: Decimal,
    provider: PaymentProvider,
    delivered_at: datetime | None = None,
) -> Purchase:
    paid = await uow.purchases.create(
        PurchaseDraft(
            user_id=INVITED,
            product_id=product.id,
            provider=provider,
            amount=amount,
            currency=provider.currency,
            external_id=uuid4().hex,
            status=PurchaseStatus.PAID,
        )
    )
    return await uow.purchases.mark_delivered(
        paid.id,
        delivered_url=DELIVERY_URL,
        delivered_at=delivered_at,
    )


async def test_only_purchases_that_earn_a_bonus_await_a_reward(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed_people(live_uow_factory)
    async with live_uow_factory() as uow:
        both = await uow.products.create(_product_draft())  # 100 ⭐ per USDT
        stars_only = await uow.products.create(_product_draft(price_usdt=None))
        usdt_only = await uow.products.create(_product_draft(price_stars=None))
        tiny_stars = await uow.products.create(_product_draft(price_usdt=None))
        tiny_usdt = await uow.products.create(_product_draft())
        stars_sale = await _delivered(
            uow, stars_only, amount=Decimal(1000), provider=PaymentProvider.STARS
        )
        crypto_sale = await _delivered(
            uow, both, amount=Decimal("10.00"), provider=PaymentProvider.CRYPTO
        )
        # Zero rewards: no Stars price to convert with, 6 ⭐ x 15% = 0.9, and
        # 0.06 USDT = 6 ⭐ x 15% = 0.9 again.
        await _delivered(uow, usdt_only, amount=Decimal("10.00"), provider=PaymentProvider.CRYPTO)
        await _delivered(uow, tiny_stars, amount=Decimal(6), provider=PaymentProvider.STARS)
        await _delivered(uow, tiny_usdt, amount=Decimal("0.06"), provider=PaymentProvider.CRYPTO)

        awaiting = await uow.bonuses.purchases_awaiting_reward(reward_percent=15)

    assert set(awaiting) == {stars_sale.id, crypto_sale.id}


async def test_nothing_awaits_a_reward_when_rewards_are_zero_percent(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed_people(live_uow_factory)
    async with live_uow_factory() as uow:
        product = await uow.products.create(_product_draft())
        await _delivered(uow, product, amount=Decimal(1000), provider=PaymentProvider.STARS)
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=0) == ()


def _python_reward(amount: Decimal, product: Product, reward_percent: int) -> int:
    """What ``BonusService`` would actually credit for this USDT sale."""
    return reward_bonuses(
        amount,
        Currency.USDT,
        policy=BonusPolicy(reward_percent=reward_percent),
        stars_per_usdt=product.stars_per_usdt,
    )


async def test_a_sale_python_rounds_to_zero_at_the_threshold_is_not_selected(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """1000 Stars / 3.00 USDT, paid 0.02, at 15%: exactly one bonus in exact maths.

    0.02 x 1000 x 15 = 300 = 100 x 3.00, so an exact-arithmetic query selects
    it. But the rate 1000/3 is held in 28 digits rounded *down* (333.33...33),
    the reward comes out as 0.99...99 and floors to 0 - so no reward is ever
    written and, before this fix, the sale came back on every pass.
    """
    await _seed_people(live_uow_factory)
    async with live_uow_factory() as uow:
        product = await uow.products.create(
            _product_draft(price_stars=1000, price_usdt=Decimal("3.00"))
        )
        await _delivered(uow, product, amount=Decimal("0.02"), provider=PaymentProvider.CRYPTO)
    assert Decimal("0.02") * 1000 * 15 == 100 * Decimal("3.00")
    assert _python_reward(Decimal("0.02"), product, 15) == 0
    service = _bonus_service(live_uow_factory, live_locks)

    async with live_uow_factory() as uow:
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=15) == ()
    assert await service.accrue_missing(limit=BATCH) == 0
    assert await service.accrue_missing(limit=BATCH) == 0


async def test_a_sale_python_rounds_up_to_one_at_the_threshold_is_still_rewarded(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """The mirror case: 1000 Stars / 24.00 USDT, paid 0.16, at 15%.

    Also exactly one bonus in exact maths, but 1000/24 is rounded *up*
    (41.66...67), so Python credits 1. A strict ``>`` in SQL would have lost it.
    """
    await _seed_people(live_uow_factory)
    async with live_uow_factory() as uow:
        product = await uow.products.create(
            _product_draft(price_stars=1000, price_usdt=Decimal("24.00"))
        )
        sale = await _delivered(
            uow, product, amount=Decimal("0.16"), provider=PaymentProvider.CRYPTO
        )
    assert Decimal("0.16") * 1000 * 15 == 100 * Decimal("24.00")
    assert _python_reward(Decimal("0.16"), product, 15) == 1
    service = _bonus_service(live_uow_factory, live_locks)

    async with live_uow_factory() as uow:
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=15) == (sale.id,)
    assert await service.accrue_missing(limit=BATCH) == 1
    async with live_uow_factory() as uow:
        assert await uow.bonuses.balance(INVITER) == 1
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=15) == ()


_THRESHOLD_RATES = (
    # (price_stars, price_usdt): rates held rounded down, rounded up, and exact.
    (1000, Decimal("3.00")),
    (2500, Decimal("7.50")),
    (1000, Decimal("24.00")),
    (1000, Decimal("6.00")),
    (2000, Decimal("3.00")),
    (100, Decimal("0.15")),
    (500, Decimal("3.00")),
    (1000, Decimal("7.00")),
    (700, Decimal("10.00")),
    (1000, Decimal("10.00")),
    (150, Decimal("2.99")),
    (2500, Decimal("7.77")),
    (1, Decimal("0.01")),
)
_THRESHOLD_PERCENTS = (10, 15, 20, 33)


def _amounts_around_the_threshold(price_stars: int, price_usdt: Decimal) -> set[Decimal]:
    """Cents on both sides of the one-bonus threshold, for every tested percent."""
    amounts: set[Decimal] = set()
    for percent in _THRESHOLD_PERCENTS:
        # Exact threshold: amount x price_stars x percent = 100 x price_usdt.
        threshold = Decimal(100) * price_usdt / (price_stars * percent)
        cents = int(threshold * 100)
        for offset in range(-2, 3):
            if cents + offset > 0:
                amounts.add(Decimal(cents + offset) / 100)
    return amounts


async def test_selection_and_reward_agree_at_the_one_bonus_threshold(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """For every rate and percent: selected exactly when ``reward_bonuses`` > 0.

    The amounts sit a few cents either side of each exact threshold, including
    the exact threshold itself wherever it is a whole cent - the only place the
    database's exact arithmetic and Python's 28-digit ``Decimal`` can differ.
    """
    await _seed_people(live_uow_factory)
    sales: list[tuple[UUID, Decimal, Product]] = []
    async with live_uow_factory() as uow:
        for price_stars, price_usdt in _THRESHOLD_RATES:
            for amount in sorted(_amounts_around_the_threshold(price_stars, price_usdt)):
                product = await uow.products.create(
                    _product_draft(price_stars=price_stars, price_usdt=price_usdt)
                )
                sale = await _delivered(
                    uow, product, amount=amount, provider=PaymentProvider.CRYPTO
                )
                sales.append((sale.id, amount, product))
        for stars in range(1, 12):
            product = await uow.products.create(_product_draft(price_usdt=None))
            sale = await _delivered(
                uow, product, amount=Decimal(stars), provider=PaymentProvider.STARS
            )
            sales.append((sale.id, Decimal(stars), product))

    rounded_down_hits = rounded_up_hits = 0
    for percent in _THRESHOLD_PERCENTS:
        expected: set[UUID] = set()
        for sale_id, amount, product in sales:
            currency = Currency.XTR if product.price_usdt is None else Currency.USDT
            earned = reward_bonuses(
                amount,
                currency,
                policy=BonusPolicy(reward_percent=percent),
                stars_per_usdt=None if currency is Currency.XTR else product.stars_per_usdt,
            )
            if earned > 0:
                expected.add(sale_id)
            on_threshold = (
                currency is Currency.USDT
                and product.price_stars is not None
                and product.price_usdt is not None
                and amount * product.price_stars * percent == 100 * product.price_usdt
            )
            if on_threshold and earned == 0:
                rounded_down_hits += 1
            elif on_threshold:
                rounded_up_hits += 1
        async with live_uow_factory() as uow:
            selected = await uow.bonuses.purchases_awaiting_reward(
                reward_percent=percent, limit=10_000
            )
        assert set(selected) == expected, percent
        assert len(selected) == len(set(selected))

    # The sweep really did land on exact thresholds, on both rounding sides.
    assert rounded_down_hits >= 2
    assert rounded_up_hits >= 2


async def test_threshold_zeros_cannot_fill_a_batch_ahead_of_a_real_reward(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """More zero-at-the-threshold sales than the batch, all older than a real one.

    They pass the database's exact pre-filter and are only dropped by the final
    check, so they must not use up the batch: the next page is read instead.
    """
    await _seed_people(live_uow_factory)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with live_uow_factory() as uow:
        for offset in range(BATCH + 5):
            product = await uow.products.create(
                _product_draft(price_stars=1000, price_usdt=Decimal("3.00"))
            )
            await _delivered(
                uow,
                product,
                amount=Decimal("0.02"),
                provider=PaymentProvider.CRYPTO,
                delivered_at=long_ago + timedelta(minutes=offset),
            )
        stars = await uow.products.create(_product_draft(price_usdt=None))
        missed = await _delivered(uow, stars, amount=Decimal(1000), provider=PaymentProvider.STARS)
    service = _bonus_service(live_uow_factory, live_locks)

    async with live_uow_factory() as uow:
        awaiting = await uow.bonuses.purchases_awaiting_reward(reward_percent=15, limit=BATCH)
    assert awaiting == (missed.id,)
    assert await service.accrue_missing(limit=BATCH) == 1
    assert await service.accrue_missing(limit=BATCH) == 0


async def test_paging_returns_at_most_the_limit_in_delivery_order(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    await _seed_people(live_uow_factory)
    start = datetime.now(UTC) - timedelta(days=1)
    expected: list[UUID] = []
    async with live_uow_factory() as uow:
        for offset in range(7):
            product = await uow.products.create(_product_draft(price_usdt=None))
            sale = await _delivered(
                uow,
                product,
                amount=Decimal(1000),
                provider=PaymentProvider.STARS,
                delivered_at=start + timedelta(minutes=offset),
            )
            expected.append(sale.id)

        first_three = await uow.bonuses.purchases_awaiting_reward(reward_percent=15, limit=3)
        everything = await uow.bonuses.purchases_awaiting_reward(reward_percent=15, limit=50)
    assert first_three == tuple(expected[:3])
    assert everything == tuple(expected)


async def test_a_zero_reward_purchase_is_not_offered_again_after_a_pass(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    await _seed_people(live_uow_factory)
    async with live_uow_factory() as uow:
        stars = await uow.products.create(_product_draft(price_usdt=None))
        crypto = await uow.products.create(_product_draft())
        usdt_only = await uow.products.create(_product_draft(price_stars=None))
        await _delivered(uow, stars, amount=Decimal(1000), provider=PaymentProvider.STARS)
        await _delivered(uow, crypto, amount=Decimal("10.00"), provider=PaymentProvider.CRYPTO)
        await _delivered(uow, usdt_only, amount=Decimal("10.00"), provider=PaymentProvider.CRYPTO)
    service = _bonus_service(live_uow_factory, live_locks)

    # 1000 ⭐ x 15% = 150, and 10.00 USDT x 100 ⭐/USDT x 15% = 150.
    assert await service.accrue_missing(limit=BATCH) == 2
    assert await service.accrue_missing(limit=BATCH) == 0
    async with live_uow_factory() as uow:
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=15) == ()
        assert await uow.bonuses.balance(INVITER) == 300


async def test_a_missed_reward_is_found_behind_a_batch_of_zero_reward_purchases(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """The failure (c) predicts for free packs: more zero-reward sales than the batch.

    They are older, so before the fix they took the whole batch on every pass and
    the newer sale that really missed its reward was never credited.
    """
    await _seed_people(live_uow_factory)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with live_uow_factory() as uow:
        for offset in range(BATCH + 5):
            usdt_only = await uow.products.create(_product_draft(price_stars=None))
            await _delivered(
                uow,
                usdt_only,
                amount=Decimal("10.00"),
                provider=PaymentProvider.CRYPTO,
                delivered_at=long_ago + timedelta(minutes=offset),
            )
        stars = await uow.products.create(_product_draft(price_usdt=None))
        missed = await _delivered(uow, stars, amount=Decimal(1000), provider=PaymentProvider.STARS)
    service = _bonus_service(live_uow_factory, live_locks)

    assert await service.accrue_missing(limit=BATCH) == 1

    async with live_uow_factory() as uow:
        reward = await uow.bonuses.find_for_purchase(
            missed.id, BonusTransactionType.REFERRAL_REWARD
        )
        assert reward is not None
        assert reward.amount == 150
        assert await uow.bonuses.purchases_awaiting_reward(reward_percent=15) == ()
