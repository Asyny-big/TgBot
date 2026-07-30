"""Unit tests for PurchaseService — business rules only, no transport."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.exceptions import (
    ConflictError,
    DuplicatePurchaseError,
    LockBusyError,
    ProductInactiveError,
    ProductNotFoundError,
    ProviderNotSupportedError,
    PurchaseNotFoundError,
    UserNotFoundError,
)
from app.domain.commands import ProductDraft, UserDraft
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.locks import payment_lock_key, purchase_lock_key
from app.services.purchases import PurchaseService
from tests.fakes import NOW, FakeLockManager, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from app.domain.entities import Product

BUYER = UserDraft(telegram_id=555001, username="buyer", first_name="Buyer", language_code="ru")


@pytest.fixture
def uow_factory() -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory()


@pytest.fixture
def locks() -> FakeLockManager:
    return FakeLockManager()


@pytest.fixture
def service(uow_factory: FakeUnitOfWorkFactory, locks: FakeLockManager) -> PurchaseService:
    return PurchaseService(uow_factory=uow_factory, locks=locks, invoice_ttl=timedelta(minutes=30))


async def _product(uow_factory: FakeUnitOfWorkFactory, **overrides: object) -> Product:
    """Seed a product and the buyer, mirroring what opening a card does."""
    await uow_factory.unit.users.upsert(BUYER)
    values: dict[str, object] = {
        "slug": "vip1",
        "title": "VIP",
        "description": "Access",
        "delivery_url": "https://t.me/+invite",
        "price_stars": 100,
        "price_usdt": Decimal("4.99"),
    }
    values.update(overrides)
    return await uow_factory.unit.products.create(ProductDraft(**values))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Opening a card
# --------------------------------------------------------------------------- #
async def test_open_card_returns_payment_options_and_remembers_the_visitor(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)

    card = await service.open_card(BUYER, product.slug)

    assert card.product == product
    assert not card.is_owned
    assert [option.provider for option in card.options] == [
        PaymentProvider.STARS,
        PaymentProvider.CRYPTO,
    ]
    assert card.options[0].amount == 100
    assert card.options[0].currency is Currency.XTR
    assert card.options[1].amount == Decimal("4.99")
    assert uow_factory.unit.users.items[BUYER.telegram_id].username == "buyer"


async def test_open_card_offers_only_priced_rails(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory, price_usdt=None)

    card = await service.open_card(BUYER, product.slug)
    assert [option.provider for option in card.options] == [PaymentProvider.STARS]


async def test_open_card_rejects_unknown_and_inactive_products(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    with pytest.raises(ProductNotFoundError):
        await service.open_card(BUYER, "nope")

    await _product(uow_factory, is_active=False)
    with pytest.raises(ProductInactiveError):
        await service.open_card(BUYER, "vip1")


async def test_open_card_returns_the_existing_purchase_for_a_repeat_buyer(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    started = await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )
    await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")

    card = await service.open_card(BUYER, product.slug)

    assert card.is_owned
    assert card.owned_purchase is not None
    assert card.owned_purchase.id == started.id


# --------------------------------------------------------------------------- #
# Starting a purchase
# --------------------------------------------------------------------------- #
async def test_start_purchase_records_a_pending_purchase(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
    locks: FakeLockManager,
) -> None:
    product = await _product(uow_factory)

    purchase = await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="cryptobot-42",
    )

    assert purchase.status is PurchaseStatus.PENDING
    assert purchase.amount == Decimal("4.99")
    assert purchase.currency is Currency.USDT
    assert purchase.external_id == "cryptobot-42"
    # The lock is scoped to this buyer and product, and it is bounded in time.
    assert locks.acquired[0][0] == purchase_lock_key(BUYER.telegram_id, product.id)
    assert locks.held == {}


async def test_start_purchase_rejects_an_unsupported_rail(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory, price_usdt=None)

    with pytest.raises(ProviderNotSupportedError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.CRYPTO,
            external_id="inv-x",
        )


async def test_start_purchase_rejects_unknown_or_inactive_products(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    await uow_factory.unit.users.upsert(BUYER)
    with pytest.raises(ProductNotFoundError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=uuid4(),
            provider=PaymentProvider.STARS,
            external_id="inv-x",
        )

    inactive = await _product(uow_factory, is_active=False)
    with pytest.raises(ProductInactiveError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=inactive.id,
            provider=PaymentProvider.STARS,
            external_id="inv-y",
        )


async def test_start_purchase_refuses_when_the_buyer_already_owns_the_product(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )
    await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")

    with pytest.raises(DuplicatePurchaseError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.CRYPTO,
            external_id="inv-2",
        )


async def test_start_purchase_requires_a_known_buyer(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    """An unseen Telegram id yields a domain error, not a foreign key crash."""
    product = await _product(uow_factory)
    uow_factory.unit.users.items.clear()

    with pytest.raises(UserNotFoundError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id="inv-1",
        )


async def test_start_purchase_gives_up_when_the_lock_is_held(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
    locks: FakeLockManager,
) -> None:
    """A second simultaneous /start must not create a second invoice."""
    product = await _product(uow_factory)
    locks.busy_keys.add(purchase_lock_key(BUYER.telegram_id, product.id))

    with pytest.raises(LockBusyError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id="inv-1",
        )
    assert uow_factory.unit.purchases.items == {}


# --------------------------------------------------------------------------- #
# Confirming payment
# --------------------------------------------------------------------------- #
async def test_confirm_payment_is_idempotent_and_never_delivers(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
    locks: FakeLockManager,
) -> None:
    product = await _product(uow_factory)
    started = await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )

    paid = await service.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="inv-1",
        telegram_charge_id="charge-1",
        paid_at=NOW,
    )
    assert paid.status is PurchaseStatus.PAID
    assert paid.paid_at == NOW
    assert paid.telegram_charge_id == "charge-1"
    # Confirming a payment must not mark anything as delivered.
    assert uow_factory.unit.purchases.items[started.id].delivered_at is None

    replay = await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")
    assert replay.status is PurchaseStatus.PAID
    assert replay.paid_at == NOW
    assert locks.acquired[-1][0] == payment_lock_key(PaymentProvider.STARS, "inv-1")


async def test_confirm_payment_for_an_unknown_invoice_raises(service: PurchaseService) -> None:
    with pytest.raises(PurchaseNotFoundError):
        await service.confirm_payment(provider=PaymentProvider.CRYPTO, external_id="ghost")


# --------------------------------------------------------------------------- #
# Delivery bookkeeping, refunds, housekeeping
# --------------------------------------------------------------------------- #
async def test_mark_delivered_requires_payment(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    started = await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )

    with pytest.raises(ConflictError):
        await service.mark_delivered(started.id, delivered_url="https://t.me/+invite")

    await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")
    delivered = await service.mark_delivered(started.id, delivered_url="https://t.me/+invite")
    assert delivered.status is PurchaseStatus.DELIVERED
    assert delivered.delivered_url == "https://t.me/+invite"


async def test_refund_revokes_access(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    started = await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )
    await service.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="inv-1",
        telegram_charge_id="charge-9",
    )

    refunded = await service.refund_by_charge_id("charge-9")
    assert refunded.status is PurchaseStatus.REFUNDED
    assert await service.find_owned(BUYER.telegram_id, product.id) is None
    assert (await service.get(started.id)).status is PurchaseStatus.REFUNDED

    with pytest.raises(PurchaseNotFoundError):
        await service.refund_by_charge_id("charge-unknown")


async def test_expire_stale_uses_the_invoice_lifetime(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )

    # Nothing is stale yet at the moment the invoice was created.
    assert await service.expire_stale(now=NOW) == 0
    # An hour later the 30 minute invoice window has passed.
    assert await service.expire_stale(now=NOW + timedelta(hours=1)) == 1
    assert await service.list_pending(PaymentProvider.STARS) == ()


async def test_every_use_case_runs_in_its_own_transaction(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    """One use case, one committed transaction — nothing is left open."""
    product = await _product(uow_factory)
    before = uow_factory.unit.commits

    await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )
    await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")

    assert uow_factory.unit.commits == before + 2
    assert uow_factory.unit.rollbacks == 0


async def test_a_failing_use_case_rolls_back(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    await uow_factory.unit.users.upsert(BUYER)
    with pytest.raises(ProductNotFoundError):
        await service.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=uuid4(),
            provider=PaymentProvider.STARS,
            external_id="inv-1",
        )
    assert uow_factory.unit.rollbacks == 1


async def test_utc_now_is_used_when_no_moment_is_supplied(
    service: PurchaseService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    product = await _product(uow_factory)
    await service.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )
    paid = await service.confirm_payment(provider=PaymentProvider.STARS, external_id="inv-1")
    assert paid.paid_at is not None
    assert paid.paid_at.tzinfo is not None
    # Housekeeping with no explicit moment must not blow up either.
    assert await service.expire_stale() >= 0
