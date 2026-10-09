"""The loyalty programme's free pack, end to end (PostgreSQL + Redis).

    (due, no row) ≡ available ─claim─▶ consumed(P),  P = internal, paid, amount 0

One transaction records the reward, inserts the internal purchase and consumes
the reward; delivery follows the commit, exactly as after a payment. These tests
drive the real services against the real database: the grant itself and its
books, every refusal, retries after a failed delivery, concurrent claims, and
the guarantees an internal purchase must keep — no invoice, no provider, no
revenue, no referral reward.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import DeliverySettings, LoyaltySettings, ReferralSettings
from app.core.exceptions import (
    DeliveryPermanentError,
    DuplicatePurchaseError,
    FreePackAlreadyUsedError,
    FreePackNotAvailableError,
    FreePackPendingCheckoutError,
    LockBusyError,
    LoyaltyDisabledError,
    ProductInactiveError,
    ProductNotFoundError,
    UserNotFoundError,
)
from app.domain.commands import (
    ProductDraft,
    ProductUpdate,
    PurchaseDraft,
    ReferralDraft,
    UserDraft,
)
from app.domain.delivery import DeliveryStatus
from app.domain.enums import (
    Currency,
    LoyaltyRewardKind,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import (
    FREE_PACK_MILESTONE,
    MilestoneRewardState,
    free_pack_external_id,
)
from app.domain.stats import StatsPeriod
from app.domain.verification import VerificationOutcome
from app.services.bonuses import BonusService
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.loyalty import LoyaltyService
from app.services.pricing import PricingService
from app.services.purchases import PurchaseService
from tests.fakes import FakeDeliveryGateway
from tests.test_loyalty_discount import RecordingStars, ScriptedCryptoPay

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import LoyaltyReward, Product, Purchase
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
    from app.services.checkout import FreePackClaim

BUYER = 9701
INVITER = 9702
DELIVERY_URL = "https://t.me/+free"
REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)
LATER = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class Shop:
    uow_factory: SqlAlchemyUnitOfWorkFactory
    bonuses: BonusService
    loyalty: LoyaltyService
    purchases: PurchaseService
    checkout: CheckoutService
    stars: RecordingStars
    crypto: ScriptedCryptoPay
    delivery: FakeDeliveryGateway


def _shop(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    locks: RedisLockManager,
    *,
    loyalty_on: bool = True,
) -> Shop:
    bonuses = BonusService(uow_factory=uow_factory, locks=locks, settings=REFERRAL)
    loyalty = LoyaltyService(uow_factory=uow_factory, settings=LoyaltySettings(enabled=loyalty_on))
    pricing = PricingService(uow_factory=uow_factory, bonuses=bonuses, loyalty=loyalty)
    purchases = PurchaseService(
        uow_factory=uow_factory,
        locks=locks,
        pricing=pricing,
        loyalty=loyalty,
    )
    gateway = FakeDeliveryGateway()
    stars = RecordingStars()
    crypto = ScriptedCryptoPay()
    checkout = CheckoutService(
        purchases=purchases,
        delivery=DeliveryService(
            uow_factory=uow_factory,
            purchases=purchases,
            gateway=gateway,
            locks=locks,
            settings=DeliverySettings(max_attempts=1, initial_backoff_seconds=0.01),
            sale_completion=bonuses,
        ),
        stars=stars,
        crypto=crypto,
    )
    return Shop(
        uow_factory=uow_factory,
        bonuses=bonuses,
        loyalty=loyalty,
        purchases=purchases,
        checkout=checkout,
        stars=stars,
        crypto=crypto,
        delivery=gateway,
    )


@pytest.fixture
def shop(live_uow_factory: SqlAlchemyUnitOfWorkFactory, live_locks: RedisLockManager) -> Shop:
    return _shop(live_uow_factory, live_locks)


async def _product(shop: Shop, *, stars: int | None = 1000, usdt: str | None = "10.00") -> Product:
    async with shop.uow_factory() as uow:
        return await uow.products.create(
            ProductDraft(
                slug=f"free{uuid4().hex[:8]}",
                title="Pack",
                description="",
                delivery_url=DELIVERY_URL,
                price_stars=stars,
                price_usdt=Decimal(usdt) if usdt is not None else None,
            )
        )


async def _buyer(shop: Shop, *, user_id: int = BUYER, received: int = FREE_PACK_MILESTONE) -> None:
    """A buyer who already received ``received`` packs — the historical kind."""
    async with shop.uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for _ in range(received):
            product = await uow.products.create(
                ProductDraft(
                    slug=f"old{uuid4().hex[:8]}",
                    title="Old pack",
                    description="",
                    delivery_url=DELIVERY_URL,
                    price_stars=100,
                )
            )
            paid = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product.id,
                    provider=PaymentProvider.STARS,
                    amount=Decimal(100),
                    currency=Currency.XTR,
                    external_id=uuid4().hex,
                    status=PurchaseStatus.PAID,
                )
            )
            await uow.purchases.mark_delivered(paid.id, delivered_url=DELIVERY_URL)


async def _free_reward(shop: Shop, user_id: int = BUYER) -> LoyaltyReward | None:
    async with shop.uow_factory() as uow:
        rewards = await uow.loyalty.rewards_of(user_id)
    found = [reward for reward in rewards if reward.milestone == FREE_PACK_MILESTONE]
    return found[0] if found else None


async def _internal_purchases(shop: Shop, user_id: int = BUYER) -> list[Purchase]:
    async with shop.uow_factory() as uow:
        rows = await uow.session.execute(
            text(
                "SELECT id FROM purchases WHERE user_id = :user AND provider = 'internal'"
                " ORDER BY created_at"
            ),
            {"user": user_id},
        )
        found = [await uow.purchases.get(row.id) for row in rows]
    return [purchase for purchase in found if purchase is not None]


async def _purchase(shop: Shop, purchase_id: UUID) -> Purchase:
    async with shop.uow_factory() as uow:
        purchase = await uow.purchases.get(purchase_id)
    assert purchase is not None
    return purchase


async def _claim(shop: Shop, product: Product, *, user_id: int = BUYER) -> FreePackClaim:
    return await shop.checkout.claim_free_pack(user_id=user_id, product=product)


# --------------------------------------------------------------------------- #
# The grant and its books                                                     #
# --------------------------------------------------------------------------- #


async def test_a_free_pack_is_granted_delivered_and_counted(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)

    claim = await _claim(shop, product)

    assert claim.delivery.status is DeliveryStatus.SENT
    assert not claim.repeated
    purchase = await _purchase(shop, claim.purchase.id)
    assert purchase.provider is PaymentProvider.INTERNAL
    assert purchase.status is PurchaseStatus.DELIVERED
    assert purchase.product_id == product.id
    # Nothing billed; the whole list price — in Stars, the first price — waived.
    assert purchase.amount == 0
    assert purchase.currency is Currency.XTR
    assert purchase.base_amount == Decimal(1000)
    assert purchase.loyalty_amount == Decimal(1000)
    assert purchase.discount_amount == purchase.bonus_amount == 0
    assert purchase.telegram_charge_id is None
    assert purchase.paid_at is not None

    reward = await _free_reward(shop)
    assert reward is not None
    assert reward.kind is LoyaltyRewardKind.FREE_PACK
    assert reward.status is LoyaltyRewardStatus.CONSUMED
    assert reward.consumed_purchase_id == purchase.id
    assert reward.applied_purchase_id is None
    assert purchase.external_id == f"loyalty:{reward.id}"

    # Handed over like any sale, and nothing was ever invoiced.
    (message,) = shop.delivery.sent
    assert message.chat_id == BUYER
    assert message.delivery_url == DELIVERY_URL
    assert shop.stars.sent == []
    assert shop.crypto.created == []

    # It counts once delivered: 11 completed, the pack is used, the 50% untouched.
    progress = await shop.loyalty.get_progress(BUYER)
    assert progress.completed_count == FREE_PACK_MILESTONE + 1
    assert progress.free_pack is MilestoneRewardState.USED
    assert progress.discount_50 is MilestoneRewardState.AVAILABLE


async def test_a_usdt_only_pack_is_valued_in_usdt(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop, stars=None, usdt="7.50")

    claim = await _claim(shop, product)

    purchase = await _purchase(shop, claim.purchase.id)
    assert (purchase.currency, purchase.base_amount, purchase.loyalty_amount) == (
        Currency.USDT,
        Decimal("7.50"),
        Decimal("7.50"),
    )
    assert purchase.amount == 0


async def test_the_card_offers_the_pack_only_while_it_waits(shop: Shop) -> None:
    await _buyer(shop)
    await _buyer(shop, user_id=BUYER + 1, received=FREE_PACK_MILESTONE - 1)
    first, second = await _product(shop), await _product(shop)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), first.slug)
    assert card.free_pack_available
    # The payment buttons stay next to it.
    assert [option.provider for option in card.options] == [
        PaymentProvider.STARS,
        PaymentProvider.CRYPTO,
    ]
    short = await shop.purchases.open_card(UserDraft(telegram_id=BUYER + 1), first.slug)
    assert not short.free_pack_available

    await _claim(shop, first)

    owned = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), first.slug)
    assert owned.is_owned
    assert not owned.free_pack_available
    other = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), second.slug)
    assert not other.free_pack_available
    # Opening cards recorded nothing for the buyer still short of the milestone.
    assert await _free_reward(shop, BUYER + 1) is None


# --------------------------------------------------------------------------- #
# Idempotency and retries                                                     #
# --------------------------------------------------------------------------- #


async def test_pressing_again_hands_over_the_same_pack(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)

    first = await _claim(shop, product)
    again = await _claim(shop, product)

    assert again.repeated
    assert again.purchase.id == first.purchase.id
    assert len(await _internal_purchases(shop)) == 1
    # The link is sent again, marked as a repeat.
    assert [message.is_repeat for message in shop.delivery.sent] == [False, True]


async def test_a_failed_delivery_is_retried_by_pressing_again(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    shop.delivery.failures = [DeliveryPermanentError("bot was blocked")]

    failed = await _claim(shop, product)

    assert failed.delivery.status is DeliveryStatus.FAILED
    held = await _purchase(shop, failed.purchase.id)
    assert held.status is PurchaseStatus.PAID
    reward = await _free_reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.CONSUMED
    # Not counted until it actually reaches the buyer.
    async with shop.uow_factory() as uow:
        assert await uow.loyalty.count_completed(BUYER) == FREE_PACK_MILESTONE

    retried = await _claim(shop, product)

    assert retried.repeated
    assert retried.purchase.id == failed.purchase.id
    assert retried.delivery.status is DeliveryStatus.SENT
    assert (await _purchase(shop, failed.purchase.id)).status is PurchaseStatus.DELIVERED
    assert len(await _internal_purchases(shop)) == 1


async def test_a_failed_delivery_is_finished_by_the_deep_link(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    shop.delivery.failures = [DeliveryPermanentError("bot was blocked")]
    failed = await _claim(shop, product)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)
    assert card.owned_purchase is not None
    assert card.owned_purchase.id == failed.purchase.id
    result = await shop.checkout.redeliver(card.owned_purchase.id)

    assert result.succeeded
    assert (await _purchase(shop, failed.purchase.id)).status is PurchaseStatus.DELIVERED
    assert len(await _internal_purchases(shop)) == 1


async def test_a_failed_delivery_is_finished_by_the_admin_verify(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    shop.delivery.failures = [DeliveryPermanentError("bot was blocked")]
    failed = await _claim(shop, product)

    report = await shop.checkout.verify_payment(failed.purchase.id)

    assert report.outcome is VerificationOutcome.DELIVERED_NOW
    again = await shop.checkout.verify_payment(failed.purchase.id)
    assert again.outcome is VerificationOutcome.ALREADY_DELIVERED
    # No provider was asked about a pack nobody paid for.
    assert shop.crypto.created == []
    assert shop.stars.sent == []


# --------------------------------------------------------------------------- #
# Refusals: nothing granted, nothing recorded                                 #
# --------------------------------------------------------------------------- #


async def test_below_the_milestone_there_is_no_pack(shop: Shop) -> None:
    await _buyer(shop, received=FREE_PACK_MILESTONE - 1)
    product = await _product(shop)

    with pytest.raises(FreePackNotAvailableError):
        await _claim(shop, product)

    assert await _internal_purchases(shop) == []
    assert await _free_reward(shop) is None


async def test_with_the_programme_off_nothing_is_granted(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    shop = _shop(live_uow_factory, live_locks, loyalty_on=False)
    await _buyer(shop, received=18)
    product = await _product(shop)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)
    assert not card.free_pack_available
    with pytest.raises(LoyaltyDisabledError):
        await _claim(shop, product)

    assert await _internal_purchases(shop) == []
    assert await _free_reward(shop) is None


async def test_a_purchase_service_without_the_programme_grants_nothing(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    bare = replace(shop.checkout, purchases=replace(shop.purchases, loyalty=None))

    with pytest.raises(LoyaltyDisabledError):
        await bare.claim_free_pack(user_id=BUYER, product=product)
    assert not (
        await bare.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)
    ).free_pack_available


async def test_an_owned_product_is_not_given_away(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    async with shop.uow_factory() as uow:
        await uow.purchases.create(
            PurchaseDraft(
                user_id=BUYER,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(1000),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=PurchaseStatus.PAID,
            )
        )

    with pytest.raises(DuplicatePurchaseError):
        await _claim(shop, product)

    assert await _internal_purchases(shop) == []
    # Rolled back with the refusal: still available, still unrecorded.
    assert await _free_reward(shop) is None


@pytest.mark.parametrize("provider", [PaymentProvider.STARS, PaymentProvider.CRYPTO])
async def test_an_unpaid_invoice_for_the_pack_blocks_it_until_it_expires(
    shop: Shop,
    provider: PaymentProvider,
) -> None:
    await _buyer(shop)
    product = await _product(shop)
    if provider is PaymentProvider.STARS:
        await shop.checkout.start_stars_checkout(user_id=BUYER, product=product)
    else:
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    with pytest.raises(FreePackPendingCheckoutError):
        await _claim(shop, product)
    assert await _internal_purchases(shop) == []
    assert await _free_reward(shop) is None

    await shop.purchases.expire_stale(now=datetime.now(UTC) + LATER)
    claim = await _claim(shop, product)

    assert claim.delivery.succeeded
    assert len(await _internal_purchases(shop)) == 1


async def test_an_unpaid_invoice_for_another_pack_does_not_block_it(shop: Shop) -> None:
    await _buyer(shop)
    elsewhere, product = await _product(shop), await _product(shop)
    await shop.checkout.start_stars_checkout(user_id=BUYER, product=elsewhere)

    claim = await _claim(shop, product)

    assert claim.delivery.succeeded


async def test_an_inactive_product_is_refused(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    async with shop.uow_factory() as uow:
        await uow.products.update(product.id, ProductUpdate(is_active=False))

    with pytest.raises(ProductInactiveError):
        await _claim(shop, product)

    assert await _internal_purchases(shop) == []
    assert await _free_reward(shop) is None


async def test_an_unknown_product_or_buyer_is_refused(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)

    with pytest.raises(ProductNotFoundError):
        await _claim(shop, replace(product, id=uuid4()))
    with pytest.raises(UserNotFoundError):
        await _claim(shop, product, user_id=BUYER + 99)
    assert await _internal_purchases(shop) == []


async def test_the_one_pack_cannot_go_to_a_second_product(shop: Shop) -> None:
    await _buyer(shop)
    first, second = await _product(shop), await _product(shop)
    await _claim(shop, first)

    with pytest.raises(FreePackAlreadyUsedError):
        await _claim(shop, second)

    (only,) = await _internal_purchases(shop)
    assert only.product_id == first.id


async def test_a_refunded_free_pack_is_not_granted_again(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    claim = await _claim(shop, product)
    await shop.purchases.refund(claim.purchase.id)

    with pytest.raises(FreePackAlreadyUsedError):
        await _claim(shop, product)

    assert len(await _internal_purchases(shop)) == 1


# --------------------------------------------------------------------------- #
# Concurrency                                                                 #
# --------------------------------------------------------------------------- #


async def test_simultaneous_presses_for_one_pack_grant_it_once(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)

    outcomes = await asyncio.gather(
        *(_claim(shop, product) for _ in range(5)),
        return_exceptions=True,
    )

    granted = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert granted
    assert all(isinstance(error, LockBusyError) for error in refused)
    (purchase,) = await _internal_purchases(shop)
    assert {claim.purchase.id for claim in granted} == {purchase.id}
    assert sum(not claim.repeated for claim in granted) == 1
    reward = await _free_reward(shop)
    assert reward is not None
    assert reward.consumed_purchase_id == purchase.id


@pytest.mark.parametrize("recorded", [False, True], ids=["first-use", "row-recorded"])
async def test_simultaneous_claims_for_different_packs_grant_one(
    shop: Shop,
    recorded: bool,
) -> None:
    """Different products take different purchase locks: the reward row decides.

    ``first-use``: no reward row yet, so the claims also meet on its insert.
    ``row-recorded``: the row already exists as available — only its lock
    (``FOR UPDATE``) stands between the claims.
    """
    await _buyer(shop)
    if recorded:
        async with shop.uow_factory() as uow:
            await uow.loyalty.ensure_reward(BUYER, FREE_PACK_MILESTONE)
    products = [await _product(shop) for _ in range(5)]

    outcomes = await asyncio.gather(
        *(_claim(shop, product) for product in products),
        return_exceptions=True,
    )

    granted = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(granted) == 1
    assert len(refused) == len(products) - 1
    assert all(isinstance(error, FreePackAlreadyUsedError) for error in refused)
    (purchase,) = await _internal_purchases(shop)
    assert purchase.id == granted[0].purchase.id


async def test_a_claim_waits_on_the_reward_another_claim_holds(shop: Shop) -> None:
    """The row lock, on its own: the window the other safeguards do not cover.

    Another claim has locked the recorded reward and inserted its pack, but not
    consumed the reward yet. A second claim must wait on the row — not read it
    as still available and race ahead — and then find the pack handed over.
    """
    await _buyer(shop)
    async with shop.uow_factory() as uow:
        await uow.loyalty.ensure_reward(BUYER, FREE_PACK_MILESTONE)
    taken, wanted = await _product(shop), await _product(shop)

    async with shop.uow_factory() as holder:
        reward = await shop.loyalty.lock_free_pack(holder, BUYER)
        assert reward is not None
        pack = await holder.purchases.create(
            PurchaseDraft(
                user_id=BUYER,
                product_id=taken.id,
                provider=PaymentProvider.INTERNAL,
                status=PurchaseStatus.PAID,
                amount=Decimal(0),
                base_amount=Decimal(1000),
                loyalty_amount=Decimal(1000),
                currency=Currency.XTR,
                external_id=free_pack_external_id(reward.id),
            )
        )
        contender = asyncio.create_task(_claim(shop, wanted))
        await asyncio.sleep(0.3)
        assert not contender.done()  # parked on the reward row
        await shop.loyalty.consume_free_pack(holder, reward, pack)

    with pytest.raises(FreePackAlreadyUsedError):
        await contender
    (only,) = await _internal_purchases(shop)
    assert only.id == pack.id


async def test_a_claim_racing_a_checkout_of_the_same_pack_never_does_both(shop: Shop) -> None:
    """Never an invoice for a pack the buyer was just given, nor the other way round."""
    for round_ in range(4):
        user_id = BUYER + 10 + round_
        await _buyer(shop, user_id=user_id)
        product = await _product(shop)

        await asyncio.gather(
            shop.checkout.start_stars_checkout(user_id=user_id, product=product),
            _claim(shop, product, user_id=user_id),
            return_exceptions=True,
        )

        async with shop.uow_factory() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        "SELECT provider::text AS provider, status::text AS status"
                        " FROM purchases WHERE user_id = :user AND product_id = :product"
                    ),
                    {"user": user_id, "product": product.id},
                )
            ).all()
        kinds = {(row.provider, row.status) for row in rows}
        given = any(provider == "internal" for provider, _ in kinds)
        invoiced = ("stars", "pending") in kinds
        # One of the two always wins — the other is refused — never both.
        assert given != invoiced, kinds


# --------------------------------------------------------------------------- #
# An internal purchase is no sale                                             #
# --------------------------------------------------------------------------- #


async def test_the_free_pack_earns_no_referral_reward_and_no_revenue(shop: Shop) -> None:
    await _buyer(shop)
    async with shop.uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=INVITER))
        await uow.referrals.create(ReferralDraft(referrer_user_id=INVITER, referred_user_id=BUYER))
        revenue_before = await uow.stats.revenue(StatsPeriod.ALL)
    product = await _product(shop)
    # The invitation predates nothing: the safety net first credits the inviter
    # for the buyer's ten paid packs. The free pack must add nothing to that.
    assert await shop.bonuses.accrue_missing() == FREE_PACK_MILESTONE

    claim = await _claim(shop, product)
    assert claim.delivery.succeeded

    async with shop.uow_factory() as uow:
        revenue_after = await uow.stats.revenue(StatsPeriod.ALL)
        rewards = (
            await uow.session.execute(
                text("SELECT count(*) FROM bonus_transactions WHERE purchase_id = :purchase"),
                {"purchase": claim.purchase.id},
            )
        ).scalar_one()
        invited_purchases = await uow.referrals.count_referral_purchases(INVITER)
    assert revenue_after == revenue_before
    assert rewards == 0
    assert invited_purchases == FREE_PACK_MILESTONE  # the paid history only
    # Nor does the safety net pick it up later.
    assert await shop.bonuses.accrue_missing() == 0


async def test_the_free_pack_leaves_the_fifty_percent_discount_alone(shop: Shop) -> None:
    await _buyer(shop)
    free, paid = await _product(shop), await _product(shop)

    await _claim(shop, free)
    purchase = await shop.checkout.start_stars_checkout(user_id=BUYER, product=paid)

    assert purchase.loyalty_amount == Decimal(500)
    assert purchase.amount == Decimal(500)
    assert shop.stars.sent[-1].amount == Decimal(500)


async def test_the_free_pack_is_never_pending_nor_reconciled(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    await _claim(shop, product)

    for provider in (PaymentProvider.STARS, PaymentProvider.CRYPTO):
        assert await shop.purchases.list_pending(provider) == ()
    # Expiry passes over it: it was never an invoice.
    assert await shop.purchases.expire_stale(now=datetime.now(UTC) + LATER) == 0
