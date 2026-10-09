"""The loyalty programme's 50% discount, end to end (PostgreSQL + Redis).

    (due, no row) ≡ available ─checkout─▶ applied(P) ─P paid─▶ consumed(P)
                                applied(P) ─P expired─▶ available

Every transition runs in the transaction that moves the purchase itself, under
a row lock on the reward, so these tests drive the real services against the
real database: Stars and CryptoBot, expiry by housekeeping and by CryptoBot,
replayed payments, concurrent checkouts, and the two Stars decisions — a
re-press replaces an unpaid discounted invoice (B), an invoice Telegram refused
to send gives its discount back at once (A).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import DeliverySettings, LoyaltySettings, ReferralSettings
from app.core.exceptions import (
    PaymentAlreadyReceivedError,
    PaymentGatewayError,
    PendingInvoiceExistsError,
)
from app.domain.commands import (
    BonusTransactionDraft,
    ProductDraft,
    ProductUpdate,
    PurchaseDraft,
    UserDraft,
)
from app.domain.enums import (
    BonusTransactionType,
    Currency,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import DISCOUNT_MILESTONE, MilestoneRewardState
from app.domain.payments import Invoice, InvoiceSnapshot, PaymentState
from app.services.bonuses import BonusService
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.loyalty import LoyaltyService
from app.services.pricing import PricingService
from app.services.purchases import PurchaseService
from tests.fakes import FakeDeliveryGateway

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from app.domain.entities import LoyaltyReward, Product, Purchase
    from app.domain.payments import InvoiceRequest
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9501
DELIVERY_URL = "https://t.me/+pack"
REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)
LATER = timedelta(hours=2)


# --------------------------------------------------------------------------- #
# Doubles for the two payment providers                                       #
# --------------------------------------------------------------------------- #


@dataclass
class RecordingStars:
    """Telegram Stars invoices, optionally refused."""

    sent: list[InvoiceRequest] = field(default_factory=list)
    fail: bool = False

    async def send_invoice(self, request: InvoiceRequest) -> None:
        if self.fail:
            message = "Telegram refused to send the invoice"
            raise PaymentGatewayError(message)
        self.sent.append(request)


@dataclass
class ScriptedCryptoPay:
    """Crypto Pay with invoice states the test drives."""

    created: list[InvoiceRequest] = field(default_factory=list)
    states: dict[str, PaymentState] = field(default_factory=dict)

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        self.created.append(request)
        external_id = f"cp{len(self.created)}-{uuid4().hex[:6]}"
        self.states[external_id] = PaymentState.PENDING
        return Invoice(
            external_id=external_id, pay_url=f"https://t.me/CryptoBot?start={external_id}"
        )

    async def fetch_states(self, external_ids: Sequence[str]) -> Mapping[str, PaymentState]:
        return {i: self.states[i] for i in external_ids if i in self.states}

    async def fetch_invoices(self, external_ids: Sequence[str]) -> Mapping[str, InvoiceSnapshot]:
        return {
            i: InvoiceSnapshot(
                external_id=i,
                state=self.states[i],
                pay_url=f"https://t.me/CryptoBot?start={i}",
            )
            for i in external_ids
            if i in self.states
        }


@dataclass(frozen=True, slots=True)
class Shop:
    uow_factory: SqlAlchemyUnitOfWorkFactory
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
    purchases = PurchaseService(uow_factory=uow_factory, locks=locks, pricing=pricing)
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


async def _product(
    shop: Shop,
    *,
    stars: int | None = 1000,
    usdt: str | None = "10.00",
) -> Product:
    async with shop.uow_factory() as uow:
        return await uow.products.create(
            ProductDraft(
                slug=f"pack{uuid4().hex[:8]}",
                title="Pack",
                description="",
                delivery_url=DELIVERY_URL,
                price_stars=stars,
                price_usdt=Decimal(usdt) if usdt is not None else None,
            )
        )


async def _buyer(
    shop: Shop,
    *,
    user_id: int = BUYER,
    received: int = DISCOUNT_MILESTONE,
    bonuses: int = 0,
) -> None:
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
        if bonuses:
            await uow.bonuses.add(
                BonusTransactionDraft(
                    user_id=user_id,
                    amount=bonuses,
                    type=BonusTransactionType.REFERRAL_REWARD,
                )
            )


async def _reward(shop: Shop, user_id: int = BUYER) -> LoyaltyReward | None:
    async with shop.uow_factory() as uow:
        rewards = await uow.loyalty.rewards_of(user_id)
    return next((r for r in rewards if r.milestone == DISCOUNT_MILESTONE), None)


async def _purchase(shop: Shop, purchase_id: UUID) -> Purchase:
    return await shop.purchases.get(purchase_id)


async def _stars_checkout(
    shop: Shop,
    product: Product,
    *,
    user_id: int = BUYER,
    use_bonus: bool = False,
) -> Purchase:
    return await shop.checkout.start_stars_checkout(
        user_id=user_id,
        product=product,
        use_bonus=use_bonus,
    )


async def _pay_stars(shop: Shop, purchase: Purchase) -> None:
    await shop.checkout.settle_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase.external_id,
        telegram_charge_id=f"charge-{purchase.id}",
    )


# --------------------------------------------------------------------------- #
# Stars and CryptoBot: apply → pay → consumed                                 #
# --------------------------------------------------------------------------- #


async def test_stars_apply_then_pay_consumes_the_reward(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    assert await _reward(shop) is None  # a historical buyer: nothing recorded yet

    purchase = await _stars_checkout(shop, product)

    assert purchase.amount == Decimal(500)
    assert purchase.base_amount == Decimal(1000)
    assert purchase.loyalty_amount == Decimal(500)
    assert purchase.discount_amount == 0
    assert purchase.bonus_amount == 0
    assert [request.amount for request in shop.stars.sent] == [Decimal(500)]
    applied = await _reward(shop)
    assert applied is not None
    assert applied.status is LoyaltyRewardStatus.APPLIED
    assert applied.applied_purchase_id == purchase.id

    await _pay_stars(shop, purchase)

    consumed = await _reward(shop)
    assert consumed is not None
    assert consumed.status is LoyaltyRewardStatus.CONSUMED
    assert consumed.consumed_purchase_id == purchase.id
    assert (await _purchase(shop, purchase.id)).status is PurchaseStatus.DELIVERED
    assert len(shop.delivery.sent) == 1

    # Used for good: the next pack is full price, and progress says so.
    next_one = await _stars_checkout(shop, await _product(shop))
    assert next_one.amount == Decimal(1000)
    assert next_one.loyalty_amount == 0
    progress = await shop.loyalty.get_progress(BUYER)
    assert progress.discount_50 is MilestoneRewardState.USED


async def test_crypto_apply_then_pay_consumes_the_reward(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)

    checkout = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    purchase = checkout.purchase
    # The amount CryptoBot bills is the amount the purchase records.
    assert [request.amount for request in shop.crypto.created] == [Decimal("5.00")]
    assert purchase.amount == Decimal("5.00")
    assert purchase.loyalty_amount == Decimal("5.00")
    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.APPLIED

    shop.crypto.states[purchase.external_id] = PaymentState.PAID
    await shop.checkout.settle_payment(
        provider=PaymentProvider.CRYPTO,
        external_id=purchase.external_id,
    )

    consumed = await _reward(shop)
    assert consumed is not None
    assert consumed.status is LoyaltyRewardStatus.CONSUMED
    assert consumed.consumed_purchase_id == purchase.id


async def test_a_replayed_payment_consumes_once_and_delivers_once(shop: Shop) -> None:
    await _buyer(shop)
    purchase = await _stars_checkout(shop, await _product(shop))

    await _pay_stars(shop, purchase)
    await _pay_stars(shop, purchase)
    await asyncio.gather(*(_pay_stars(shop, purchase) for _ in range(3)), return_exceptions=True)

    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.CONSUMED
    assert reward.consumed_purchase_id == purchase.id
    assert len(shop.delivery.sent) == 1


# --------------------------------------------------------------------------- #
# Expiry gives the reward back                                                #
# --------------------------------------------------------------------------- #


async def test_housekeeping_releases_the_reward_of_an_expired_invoice(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    first = await _stars_checkout(shop, product)

    assert await shop.purchases.expire_stale(now=datetime.now(UTC) + LATER) == 1
    assert await shop.loyalty.release_stale_discounts() == 1
    # Released once: the next pass finds nothing to do.
    assert await shop.loyalty.release_stale_discounts() == 0

    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.AVAILABLE
    assert reward.applied_purchase_id is None
    assert (await _purchase(shop, first.id)).status is PurchaseStatus.EXPIRED

    # A new checkout gets the discount again, on the new purchase.
    second = await _stars_checkout(shop, product)
    assert second.loyalty_amount == Decimal(500)
    again = await _reward(shop)
    assert again is not None
    assert again.applied_purchase_id == second.id


async def test_a_crypto_invoice_expired_at_cryptobot_hands_the_discount_to_the_next(
    shop: Shop,
) -> None:
    await _buyer(shop)
    product = await _product(shop)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.states[first.purchase.external_id] = PaymentState.EXPIRED

    second = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert not second.resumed
    assert second.purchase.id != first.purchase.id
    assert second.purchase.amount == Decimal("5.00")
    assert [request.amount for request in shop.crypto.created] == [
        Decimal("5.00"),
        Decimal("5.00"),
    ]
    assert (await _purchase(shop, first.purchase.id)).status is PurchaseStatus.EXPIRED
    reward = await _reward(shop)
    assert reward is not None
    assert reward.applied_purchase_id == second.purchase.id
    async with shop.uow_factory() as uow:
        pending = await uow.purchases.pending_checkouts(BUYER, product.id, PaymentProvider.CRYPTO)
    assert [purchase.id for purchase in pending] == [second.purchase.id]


# --------------------------------------------------------------------------- #
# Bug #3 with a discounted invoice                                            #
# --------------------------------------------------------------------------- #


async def test_pressing_crypto_again_hands_back_the_discounted_invoice(shop: Shop) -> None:
    await _buyer(shop, bonuses=300)
    product = await _product(shop)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    again = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    # Asking for bonuses changes nothing: they never combine with the discount.
    with_bonus = await shop.checkout.start_crypto_checkout(
        user_id=BUYER,
        product=product,
        use_bonus=True,
    )

    assert again.resumed
    assert with_bonus.resumed
    assert again.purchase.id == with_bonus.purchase.id == first.purchase.id
    assert len(shop.crypto.created) == 1
    reward = await _reward(shop)
    assert reward is not None
    assert reward.applied_purchase_id == first.purchase.id


async def test_a_paid_discounted_crypto_invoice_is_not_followed_by_another(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.states[first.purchase.external_id] = PaymentState.PAID

    with pytest.raises(PaymentAlreadyReceivedError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    reward = await _reward(shop)
    assert reward is not None
    assert reward.applied_purchase_id == first.purchase.id


async def test_a_discounted_invoice_at_an_old_list_price_is_not_handed_back(shop: Shop) -> None:
    await _buyer(shop)
    product = await _product(shop)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    async with shop.uow_factory() as uow:
        repriced = await uow.products.update(product.id, ProductUpdate(price_usdt=Decimal("12.00")))

    with pytest.raises(PendingInvoiceExistsError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=repriced)

    assert len(shop.crypto.created) == 1
    reward = await _reward(shop)
    assert reward is not None
    assert reward.applied_purchase_id == first.purchase.id


# --------------------------------------------------------------------------- #
# The two Stars decisions                                                     #
# --------------------------------------------------------------------------- #


async def test_a_refused_stars_invoice_gives_the_discount_back_at_once(shop: Shop) -> None:
    """Decision A: Telegram refused to send it, so nobody can pay it."""
    await _buyer(shop)
    product = await _product(shop)
    shop.stars.fail = True

    with pytest.raises(PaymentGatewayError):
        await _stars_checkout(shop, product)

    async with shop.uow_factory() as uow:
        withdrawn = await uow.purchases.pending_checkouts(BUYER, product.id, PaymentProvider.STARS)
    assert withdrawn == ()
    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.AVAILABLE

    shop.stars.fail = False
    retried = await _stars_checkout(shop, product)
    assert retried.loyalty_amount == Decimal(500)


async def test_a_refused_plain_stars_invoice_behaves_as_before(shop: Shop) -> None:
    """Without the discount nothing changed: the pending purchase simply expires later."""
    await _buyer(shop, received=0)
    product = await _product(shop)
    shop.stars.fail = True

    with pytest.raises(PaymentGatewayError):
        await _stars_checkout(shop, product)

    async with shop.uow_factory() as uow:
        pending = await uow.purchases.pending_checkouts(BUYER, product.id, PaymentProvider.STARS)
    assert len(pending) == 1
    assert pending[0].loyalty_amount == 0


async def test_pressing_stars_again_moves_the_discount_to_the_new_invoice(shop: Shop) -> None:
    """Decision B: the unpaid discounted invoice is replaced, never doubled."""
    await _buyer(shop)
    product = await _product(shop)
    first = await _stars_checkout(shop, product)

    second = await _stars_checkout(shop, product)

    assert (await _purchase(shop, first.id)).status is PurchaseStatus.EXPIRED
    assert second.loyalty_amount == Decimal(500)
    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.APPLIED
    assert reward.applied_purchase_id == second.id

    await _pay_stars(shop, second)
    consumed = await _reward(shop)
    assert consumed is not None
    assert consumed.consumed_purchase_id == second.id


async def test_pressing_stars_again_without_a_discount_behaves_as_before(shop: Shop) -> None:
    await _buyer(shop, received=0)
    product = await _product(shop)
    first = await _stars_checkout(shop, product)

    await _stars_checkout(shop, product)

    assert (await _purchase(shop, first.id)).status is PurchaseStatus.PENDING


# --------------------------------------------------------------------------- #
# Precedence and the edges of the arithmetic                                  #
# --------------------------------------------------------------------------- #


async def test_bonuses_asked_for_are_dropped_in_favour_of_the_discount(shop: Shop) -> None:
    """Decision C: loyalty wins, bonuses are neither combined nor reserved."""
    await _buyer(shop, bonuses=300)

    purchase = await _stars_checkout(shop, await _product(shop), use_bonus=True)

    assert purchase.amount == Decimal(500)
    assert purchase.bonus_amount == 0
    async with shop.uow_factory() as uow:
        assert await uow.bonuses.balance(BUYER) == 300
        assert await uow.bonuses.find_for_purchase(purchase.id) is None


async def test_with_the_programme_off_nothing_is_discounted_or_recorded(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    shop = _shop(live_uow_factory, live_locks, loyalty_on=False)
    await _buyer(shop, received=7)

    stars = await _stars_checkout(shop, await _product(shop))
    crypto = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=await _product(shop))

    assert stars.amount == Decimal(1000)
    assert crypto.purchase.amount == Decimal("10.00")
    assert await _reward(shop) is None


@pytest.mark.parametrize(
    ("stars", "usdt", "provider", "charged"),
    [
        (1, None, PaymentProvider.STARS, Decimal(1)),
        (None, "0.01", PaymentProvider.CRYPTO, Decimal("0.01")),
    ],
    ids=["one-star", "one-cent"],
)
async def test_a_price_too_small_to_halve_does_not_touch_the_reward(
    shop: Shop,
    stars: int | None,
    usdt: str | None,
    provider: PaymentProvider,
    charged: Decimal,
) -> None:
    await _buyer(shop)
    product = await _product(shop, stars=stars, usdt=usdt)

    if provider is PaymentProvider.STARS:
        purchase = await _stars_checkout(shop, product)
    else:
        purchase = (
            await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
        ).purchase

    assert purchase.amount == charged
    assert purchase.loyalty_amount == 0
    # Not even recorded: the discount was never going to be used here.
    assert await _reward(shop) is None
    progress = await shop.loyalty.get_progress(BUYER)
    assert progress.discount_50_available


# --------------------------------------------------------------------------- #
# Concurrency                                                                 #
# --------------------------------------------------------------------------- #


async def test_concurrent_checkouts_of_different_packs_share_one_discount(shop: Shop) -> None:
    """Different packs hold different purchase locks; the reward row lock decides."""
    await _buyer(shop)
    products = [await _product(shop) for _ in range(4)]

    purchases = await asyncio.gather(*(_stars_checkout(shop, product) for product in products))

    discounted = [purchase for purchase in purchases if purchase.loyalty_amount > 0]
    assert len(discounted) == 1
    assert sorted(purchase.amount for purchase in purchases) == [
        Decimal(500),
        Decimal(1000),
        Decimal(1000),
        Decimal(1000),
    ]
    reward = await _reward(shop)
    assert reward is not None
    assert reward.applied_purchase_id == discounted[0].id


async def test_payment_racing_expiry_ends_consumed_exactly_once(shop: Shop) -> None:
    """Whichever lands first, the paid purchase ends up holding the consumed reward."""
    for round_number in range(4):
        user_id = BUYER + 100 + round_number
        await _buyer(shop, user_id=user_id)
        purchase = await shop.checkout.start_stars_checkout(
            user_id=user_id,
            product=await _product(shop),
        )

        async def expire_and_release() -> None:
            await shop.purchases.expire_stale(now=datetime.now(UTC) + LATER)
            await shop.loyalty.release_stale_discounts()

        await asyncio.gather(
            _pay_stars(shop, purchase),
            expire_and_release(),
            return_exceptions=True,
        )
        await shop.loyalty.release_stale_discounts()

        settled = await _purchase(shop, purchase.id)
        reward = await _reward(shop, user_id)
        assert settled.status in (PurchaseStatus.PAID, PurchaseStatus.DELIVERED)
        assert reward is not None
        assert reward.status is LoyaltyRewardStatus.CONSUMED
        assert reward.consumed_purchase_id == purchase.id


async def test_a_late_payment_after_release_is_honoured_not_broken(shop: Shop) -> None:
    """Money arrived for an invoice whose discount was released: deliver, consume, log."""
    await _buyer(shop)
    purchase = await _stars_checkout(shop, await _product(shop))
    await shop.purchases.expire_stale(now=datetime.now(UTC) + LATER)
    await shop.loyalty.release_stale_discounts()

    await _pay_stars(shop, purchase)

    assert (await _purchase(shop, purchase.id)).status is PurchaseStatus.DELIVERED
    reward = await _reward(shop)
    assert reward is not None
    assert reward.status is LoyaltyRewardStatus.CONSUMED
    assert reward.consumed_purchase_id == purchase.id


async def test_the_discount_never_creates_an_internal_purchase(shop: Shop) -> None:
    """INTERNAL is the free pack's rail; the 50% discount is paid through Stars or CryptoBot."""
    await _buyer(shop)
    purchase = await _stars_checkout(shop, await _product(shop))
    await _pay_stars(shop, purchase)

    async with shop.uow_factory() as uow:
        internal = await uow.session.scalar(
            text("SELECT count(*) FROM purchases WHERE provider::text = 'internal'")
        )
    assert internal == 0
    assert purchase.provider is PaymentProvider.STARS
