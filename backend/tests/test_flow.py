"""End-to-end service tests on real PostgreSQL and real Redis.

These are the scenarios that decide whether the shop can be trusted with money:
the whole purchase flow, and what happens when two things arrive at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import DeliverySettings, TelegramSettings
from app.core.exceptions import DeliveryTransientError, DuplicatePurchaseError, LockBusyError
from app.domain.commands import ProductDraft, UserDraft
from app.domain.delivery import DeliveryResult, DeliveryStatus
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.pagination import PageRequest, PurchaseFilters
from app.services.delivery import DeliveryService
from app.services.products import ProductService
from app.services.purchases import PurchaseService
from app.services.stats import StatsService
from tests.conftest import VALID_BOT_TOKEN
from tests.fakes import FakeDeliveryGateway

if TYPE_CHECKING:
    from app.domain.entities import Product
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = UserDraft(telegram_id=9001, username="buyer", first_name="Buyer", language_code="ru")
OTHER_BUYER = UserDraft(telegram_id=9002, username="other", first_name="Other", language_code="en")
DELIVERY_URL = "https://t.me/+private-invite"


@dataclass(frozen=True, slots=True)
class Shop:
    """Every service wired over the live database and live Redis."""

    products: ProductService
    purchases: PurchaseService
    delivery: DeliveryService
    stats: StatsService
    gateway: FakeDeliveryGateway


@pytest.fixture
def shop(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> Shop:
    telegram = TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username="MyShopBot",
        use_webhook=False,
        webhook_secret=SecretStr("webhook-secret-value"),
    )
    purchases = PurchaseService(uow_factory=live_uow_factory, locks=live_locks)
    gateway = FakeDeliveryGateway()
    return Shop(
        products=ProductService(uow_factory=live_uow_factory, telegram=telegram),
        purchases=purchases,
        delivery=DeliveryService(
            uow_factory=live_uow_factory,
            purchases=purchases,
            gateway=gateway,
            locks=live_locks,
            settings=DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
        ),
        stats=StatsService(uow_factory=live_uow_factory),
        gateway=gateway,
    )


async def _buyers(shop: Shop, *profiles: UserDraft) -> None:
    """The bot records a visitor when the card opens; mirror that here."""
    for profile in profiles:
        await shop.purchases.remember_user(profile)


async def _product(shop: Shop, **overrides: object) -> Product:
    values: dict[str, object] = {
        "slug": f"vip{uuid4().hex[:6]}",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": DELIVERY_URL,
        "price_stars": 150,
        "price_usdt": Decimal("5.00"),
    }
    values.update(overrides)
    return await shop.products.create(ProductDraft(**values))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The happy path, end to end
# --------------------------------------------------------------------------- #
async def test_full_purchase_flow_from_deep_link_to_repeat_visit(shop: Shop) -> None:
    product = await _product(shop)

    # 1. The deep link is opened: the card offers both rails.
    card = await shop.purchases.open_card(BUYER, product.slug)
    assert not card.is_owned
    assert [option.provider for option in card.options] == [
        PaymentProvider.STARS,
        PaymentProvider.CRYPTO,
    ]

    # 2. An invoice is created, then paid.
    pending = await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-1",
    )
    assert pending.status is PurchaseStatus.PENDING

    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-payload-1",
        telegram_charge_id="charge-1",
    )
    assert paid.status is PurchaseStatus.PAID
    # Confirming the payment did not send anything: that is delivery's job.
    assert shop.gateway.sent == []

    # 3. Delivery hands the link over and confirms the purchase.
    result = await shop.delivery.deliver_purchase(paid.id)
    assert result.status is DeliveryStatus.SENT
    assert shop.gateway.sent[0].delivery_url == DELIVERY_URL
    assert (await shop.purchases.get(paid.id)).status is PurchaseStatus.DELIVERED

    # 4. The buyer opens the deep link again: no payment, the link is re-sent.
    repeat_card = await shop.purchases.open_card(BUYER, product.slug)
    assert repeat_card.is_owned
    assert repeat_card.owned_purchase is not None
    repeat = await shop.delivery.redeliver(repeat_card.owned_purchase.id)
    assert repeat.status is DeliveryStatus.SENT
    assert len(shop.gateway.sent) == 2
    assert shop.gateway.sent[1].is_repeat

    # 5. The dashboard sees exactly one sale.
    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 1
    assert overview.total.stars_amount == 150
    assert overview.total.usdt_amount == Decimal(0)
    assert overview.users_total == 1
    assert overview.products_active == 1
    assert [top.slug for top in overview.top_products] == [product.slug]
    assert overview.recent_purchases[0].user.telegram_id == BUYER.telegram_id


async def test_crypto_purchase_is_charged_in_usdt(shop: Shop) -> None:
    product = await _product(shop)
    await _buyers(shop, BUYER)

    pending = await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="cryptobot-777",
    )
    assert pending.currency is Currency.USDT
    assert pending.amount == Decimal("5.00")

    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.CRYPTO,
        external_id="cryptobot-777",
    )
    await shop.delivery.deliver_purchase(paid.id)

    overview = await shop.stats.overview()
    assert overview.total.usdt_amount == Decimal("5.00")
    assert overview.total.stars_amount == 0


async def test_a_refund_lets_the_product_be_sold_again(shop: Shop) -> None:
    product = await _product(shop)
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-1",
    )
    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-1",
        telegram_charge_id="charge-refundable",
    )
    await shop.delivery.deliver_purchase(paid.id)

    await shop.purchases.refund_by_charge_id("charge-refundable")
    card = await shop.purchases.open_card(BUYER, product.slug)
    assert not card.is_owned

    # A refunded sale no longer counts as revenue.
    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 0

    # And the buyer may purchase it again.
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-2",
    )
    second = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-2",
    )
    assert second.status is PurchaseStatus.PAID


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
async def test_two_simultaneous_start_presses_create_one_invoice(shop: Shop) -> None:
    """The TTL-bounded Redis lock keeps a double tap from issuing two invoices."""
    product = await _product(shop)
    await _buyers(shop, BUYER)

    async def start(external_id: str) -> object:
        try:
            return await shop.purchases.start_purchase(
                user_id=BUYER.telegram_id,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                external_id=external_id,
            )
        except LockBusyError as error:
            return error

    first, second = await asyncio.gather(start("inv-a"), start("inv-b"))

    outcomes = [first, second]
    busy = [item for item in outcomes if isinstance(item, LockBusyError)]
    created = [item for item in outcomes if not isinstance(item, LockBusyError)]
    assert len(busy) == 1
    assert len(created) == 1

    pending = await shop.purchases.list_pending(PaymentProvider.STARS)
    assert len(pending) == 1


async def test_a_replayed_payment_webhook_is_confirmed_once(shop: Shop) -> None:
    """Ten copies of the same callback must produce one paid purchase."""
    product = await _product(shop)
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="cryptobot-replay",
    )

    async def confirm() -> object:
        return await shop.purchases.confirm_payment(
            provider=PaymentProvider.CRYPTO,
            external_id="cryptobot-replay",
        )

    results = await asyncio.gather(*(confirm() for _ in range(10)))

    assert all(getattr(item, "status", None) is PurchaseStatus.PAID for item in results)
    paid_at = {getattr(item, "paid_at", None) for item in results}
    assert len(paid_at) == 1  # every caller sees the same confirmation moment

    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 1


async def test_parallel_deliveries_send_the_link_once(shop: Shop) -> None:
    product = await _product(shop)
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-parallel",
    )
    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-parallel",
    )

    async def deliver() -> DeliveryResult | LockBusyError:
        try:
            return await shop.delivery.deliver_purchase(paid.id)
        except LockBusyError as error:
            return error

    results = await asyncio.gather(*(deliver() for _ in range(5)))

    assert len(shop.gateway.sent) == 1
    statuses = {item.status for item in results if isinstance(item, DeliveryResult)}
    assert statuses <= {DeliveryStatus.SENT, DeliveryStatus.ALREADY_DELIVERED}
    assert (await shop.purchases.get(paid.id)).status is PurchaseStatus.DELIVERED


async def test_two_invoices_for_one_product_can_only_be_paid_once(shop: Shop) -> None:
    """Stars and CryptoBot invoices race; the database allows exactly one to win."""
    product = await _product(shop)
    await _buyers(shop, BUYER)

    stars = await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-race",
    )
    crypto = await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="crypto-race",
    )
    assert stars.id != crypto.id

    async def confirm(provider: PaymentProvider, external_id: str) -> object:
        try:
            return await shop.purchases.confirm_payment(
                provider=provider,
                external_id=external_id,
            )
        except DuplicatePurchaseError as error:
            return error

    results = await asyncio.gather(
        confirm(PaymentProvider.STARS, "stars-race"),
        confirm(PaymentProvider.CRYPTO, "crypto-race"),
    )

    rejected = [item for item in results if isinstance(item, DuplicatePurchaseError)]
    accepted = [item for item in results if not isinstance(item, DuplicatePurchaseError)]
    assert len(rejected) == 1
    assert len(accepted) == 1

    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 1


async def test_different_buyers_are_not_blocked_by_each_other(shop: Shop) -> None:
    product = await _product(shop)

    async def buy(profile: UserDraft, external_id: str) -> None:
        await shop.purchases.open_card(profile, product.slug)
        await shop.purchases.start_purchase(
            user_id=profile.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id=external_id,
        )
        paid = await shop.purchases.confirm_payment(
            provider=PaymentProvider.STARS,
            external_id=external_id,
        )
        result = await shop.delivery.deliver_purchase(paid.id)
        assert result.status is DeliveryStatus.SENT

    await asyncio.gather(buy(BUYER, "inv-first"), buy(OTHER_BUYER, "inv-second"))

    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 2
    assert overview.users_total == 2
    assert len(shop.gateway.sent) == 2


async def test_delivery_failure_keeps_the_purchase_recoverable(shop: Shop) -> None:
    """A dead transport must not lose the buyer's entitlement."""
    product = await _product(shop)
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-broken",
    )
    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-broken",
    )

    shop.gateway.failures = [
        DeliveryTransientError("telegram down"),
        DeliveryTransientError("telegram still down"),
    ]
    failed = await shop.delivery.deliver_purchase(paid.id)
    assert failed.status is DeliveryStatus.FAILED
    assert (await shop.purchases.get(paid.id)).status is PurchaseStatus.PAID

    # Telegram recovers, the retry succeeds and the purchase is completed.
    recovered = await shop.delivery.deliver_purchase(paid.id)
    assert recovered.status is DeliveryStatus.SENT
    assert (await shop.purchases.get(paid.id)).status is PurchaseStatus.DELIVERED


async def test_admin_search_finds_the_sale(shop: Shop) -> None:
    product = await _product(shop, title="Python course")
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="INV-SEARCH-1",
    )
    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="INV-SEARCH-1",
        telegram_charge_id="charge-search",
    )
    await shop.delivery.deliver_purchase(paid.id)

    for term in (str(BUYER.telegram_id), "@buyer", "python", "inv-search-1", "charge-search"):
        page = await shop.stats.search_purchases(PurchaseFilters(search=term), PageRequest())
        assert [record.purchase.id for record in page.items] == [paid.id], term

    empty = await shop.stats.search_purchases(PurchaseFilters(search="nope"), PageRequest())
    assert empty.total == 0


async def test_refund_by_purchase_id_revokes_access(shop: Shop) -> None:
    product = await _product(shop)
    await _buyers(shop, BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-direct-refund",
    )
    paid = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-direct-refund",
    )

    refunded = await shop.purchases.refund(paid.id)
    assert refunded.status is PurchaseStatus.REFUNDED
    assert await shop.purchases.find_owned(BUYER.telegram_id, product.id) is None


async def test_expiring_stale_invoices_leaves_paid_ones_alone(shop: Shop) -> None:
    product = await _product(shop)
    await _buyers(shop, BUYER, OTHER_BUYER)
    await shop.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-stale",
    )
    await shop.purchases.start_purchase(
        user_id=OTHER_BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-fresh",
    )
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-fresh",
    )

    # Every invoice older than "now + 1s" is stale, i.e. all pending ones.
    expired = await shop.purchases.expire_stale(now=datetime.now(UTC) + timedelta(hours=1))
    assert expired == 1
    assert await shop.purchases.list_pending(PaymentProvider.STARS) == ()

    overview = await shop.stats.overview()
    assert overview.total.purchases_count == 1
