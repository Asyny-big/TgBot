"""Integration tests for the purchase repository (real PostgreSQL).

These cover the money-critical paths: idempotent payment confirmation, the
"one paid copy per buyer" rule, delivery bookkeeping and refunds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.exceptions import ConflictError, DuplicatePurchaseError, PurchaseNotFoundError
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.pagination import PageRequest, PurchaseFilters
from tests.db import product_draft, purchase_draft, seed_paid_purchase, user_draft

if TYPE_CHECKING:
    from app.domain.entities import Product, User
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

DELIVERY_URL = "https://t.me/+secret-invite"


@pytest.fixture
async def buyer(users: SqlAlchemyUserRepository) -> User:
    return await users.upsert(user_draft(500100200))


@pytest.fixture
async def product(products: SqlAlchemyProductRepository) -> Product:
    return await products.create(product_draft(slug="vip1"))


async def test_create_pending_purchase(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    created = await purchases.create(purchase_draft(buyer, product, external_id="inv-1"))

    assert created.status is PurchaseStatus.PENDING
    assert created.provider is PaymentProvider.STARS
    assert created.currency is Currency.XTR
    assert created.amount == Decimal("100")
    assert created.paid_at is None
    assert await purchases.get(created.id) == created
    assert await purchases.get_by_external_id(PaymentProvider.STARS, "inv-1") == created
    assert await purchases.get_by_external_id(PaymentProvider.CRYPTO, "inv-1") is None


async def test_same_invoice_cannot_be_recorded_twice(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    await purchases.create(purchase_draft(buyer, product, external_id="inv-dup"))

    with pytest.raises(ConflictError, match="already recorded"):
        await purchases.create(purchase_draft(buyer, product, external_id="inv-dup"))

    # The same id under another provider is a different invoice and is allowed.
    other = await purchases.create(
        purchase_draft(buyer, product, PaymentProvider.CRYPTO, external_id="inv-dup")
    )
    assert other.provider is PaymentProvider.CRYPTO


async def test_mark_paid_is_idempotent(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    created = await purchases.create(purchase_draft(buyer, product))

    paid = await purchases.mark_paid(created.id, telegram_charge_id="charge-1")
    assert paid.status is PurchaseStatus.PAID
    assert paid.paid_at is not None
    assert paid.telegram_charge_id == "charge-1"

    replay = await purchases.mark_paid(created.id, telegram_charge_id="charge-1")
    assert replay.paid_at == paid.paid_at
    assert replay.status is PurchaseStatus.PAID


async def test_second_paid_copy_of_the_same_product_is_refused(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    """The database, not the application, guarantees one paid copy per buyer."""
    await seed_paid_purchase(purchases, buyer, product)

    second = await purchases.create(
        purchase_draft(buyer, product, PaymentProvider.CRYPTO, external_id="inv-second")
    )
    with pytest.raises(DuplicatePurchaseError):
        await purchases.mark_paid(second.id)


async def test_find_access_granting_returns_the_paid_purchase(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    assert await purchases.find_access_granting(buyer.telegram_id, product.id) is None

    pending = await purchases.create(purchase_draft(buyer, product))
    assert await purchases.find_access_granting(buyer.telegram_id, product.id) is None

    paid = await purchases.mark_paid(pending.id)
    found = await purchases.find_access_granting(buyer.telegram_id, product.id)
    assert found is not None
    assert found.id == paid.id


async def test_delivery_requires_payment_and_is_idempotent(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    created = await purchases.create(purchase_draft(buyer, product))

    with pytest.raises(ConflictError, match="Only a paid purchase"):
        await purchases.mark_delivered(created.id, delivered_url=DELIVERY_URL)

    await purchases.mark_paid(created.id)
    delivered = await purchases.mark_delivered(created.id, delivered_url=DELIVERY_URL)
    assert delivered.status is PurchaseStatus.DELIVERED
    assert delivered.delivered_url == DELIVERY_URL
    assert delivered.delivered_at is not None

    replay = await purchases.mark_delivered(created.id, delivered_url="https://other")
    assert replay.delivered_url == DELIVERY_URL
    assert replay.delivered_at == delivered.delivered_at


async def test_refund_revokes_access_and_blocks_repayment(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    paid = await seed_paid_purchase(purchases, buyer, product)

    refunded = await purchases.mark_refunded(paid.id)
    assert refunded.status is PurchaseStatus.REFUNDED
    assert refunded.paid_at is not None
    assert await purchases.find_access_granting(buyer.telegram_id, product.id) is None

    assert (await purchases.mark_refunded(paid.id)).status is PurchaseStatus.REFUNDED
    with pytest.raises(ConflictError, match="Refunded purchase"):
        await purchases.mark_paid(paid.id)


async def test_after_refund_the_product_can_be_bought_again(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    first = await seed_paid_purchase(purchases, buyer, product)
    await purchases.mark_refunded(first.id)

    second = await purchases.create(purchase_draft(buyer, product, external_id="inv-again"))
    assert (await purchases.mark_paid(second.id)).status is PurchaseStatus.PAID


async def test_unknown_purchase_raises(purchases: SqlAlchemyPurchaseRepository) -> None:
    missing = uuid4()
    assert await purchases.get(missing) is None
    with pytest.raises(PurchaseNotFoundError):
        await purchases.mark_paid(missing)
    with pytest.raises(PurchaseNotFoundError):
        await purchases.mark_delivered(missing, delivered_url=DELIVERY_URL)
    with pytest.raises(PurchaseNotFoundError):
        await purchases.mark_refunded(missing)


async def test_expire_pending_only_touches_stale_rows(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    stale = await purchases.create(purchase_draft(buyer, product, external_id="inv-stale"))
    paid = await seed_paid_purchase(
        purchases, buyer, product, PaymentProvider.CRYPTO, external_id="inv-paid"
    )

    expired_count = await purchases.expire_pending(datetime.now(UTC) + timedelta(seconds=1))
    assert expired_count == 1

    refreshed = await purchases.get(stale.id)
    assert refreshed is not None
    assert refreshed.status is PurchaseStatus.EXPIRED
    still_paid = await purchases.get(paid.id)
    assert still_paid is not None
    assert still_paid.status is PurchaseStatus.PAID


async def test_expired_invoice_can_still_be_paid_late(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    """Money that arrives after the invoice expired must still be honoured."""
    created = await purchases.create(purchase_draft(buyer, product))
    await purchases.expire_pending(datetime.now(UTC) + timedelta(seconds=1))

    paid = await purchases.mark_paid(created.id)
    assert paid.status is PurchaseStatus.PAID


async def test_list_pending_is_scoped_per_provider(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    stars = await purchases.create(purchase_draft(buyer, product, external_id="inv-stars"))
    crypto = await purchases.create(
        purchase_draft(buyer, product, PaymentProvider.CRYPTO, external_id="inv-crypto")
    )
    await purchases.mark_paid(stars.id)

    pending_crypto = await purchases.list_pending(PaymentProvider.CRYPTO)
    assert [item.id for item in pending_crypto] == [crypto.id]
    assert await purchases.list_pending(PaymentProvider.STARS) == ()


async def test_search_matches_every_documented_field(
    purchases: SqlAlchemyPurchaseRepository,
    products: SqlAlchemyProductRepository,
    users: SqlAlchemyUserRepository,
) -> None:
    buyer = await users.upsert(user_draft(777888999, username="VipBuyer"))
    other = await users.upsert(user_draft(111222333, username="someone"))
    course = await products.create(product_draft(slug="course", title="Python course"))
    pack = await products.create(product_draft(slug="pack18", title="Sticker pack"))

    target = await purchases.create(
        purchase_draft(buyer, course, external_id="INV-4242", status=None)
    )
    await purchases.mark_paid(target.id, telegram_charge_id="charge-XYZ")
    await purchases.create(purchase_draft(other, pack, external_id="INV-0001"))

    async def search(term: str) -> tuple[str, ...]:
        page = await purchases.search(PurchaseFilters(search=term), PageRequest())
        return tuple(str(record.purchase.id) for record in page.items)

    assert await search("777888999") == (str(target.id),)
    assert await search("@vipbuyer") == (str(target.id),)
    assert await search("python") == (str(target.id),)
    assert await search("course") == (str(target.id),)
    assert await search("inv-4242") == (str(target.id),)
    assert await search("charge-xyz") == (str(target.id),)
    assert await search("nothing-matches") == ()

    everything = await purchases.search(PurchaseFilters(), PageRequest())
    assert everything.total == 2
    record = everything.items[0]
    assert record.user.display_name.startswith("@")
    assert record.product.slug in {"course", "pack18"}


async def test_search_can_filter_by_status(
    purchases: SqlAlchemyPurchaseRepository,
    buyer: User,
    product: Product,
) -> None:
    pending = await purchases.create(purchase_draft(buyer, product, external_id="inv-pending"))
    paid = await seed_paid_purchase(
        purchases, buyer, product, PaymentProvider.CRYPTO, external_id="inv-ok"
    )

    only_paid = await purchases.search(
        PurchaseFilters(statuses=(PurchaseStatus.PAID.value,)),
        PageRequest(),
    )
    assert [record.purchase.id for record in only_paid.items] == [paid.id]

    only_pending = await purchases.search(
        PurchaseFilters(statuses=(PurchaseStatus.PENDING.value,)),
        PageRequest(),
    )
    assert [record.purchase.id for record in only_pending.items] == [pending.id]


async def test_the_same_telegram_charge_cannot_be_stored_twice(
    purchases: SqlAlchemyPurchaseRepository,
    products: SqlAlchemyProductRepository,
    users: SqlAlchemyUserRepository,
    buyer: User,
    product: Product,
) -> None:
    """A Telegram charge id identifies one payment, so it must stay unique."""
    first = await purchases.create(purchase_draft(buyer, product, external_id="inv-charge-1"))
    await purchases.mark_paid(first.id, telegram_charge_id="charge-unique")

    other_buyer = await users.upsert(user_draft(900900900))
    other_product = await products.create(product_draft(slug="another"))
    second = await purchases.create(
        purchase_draft(other_buyer, other_product, external_id="inv-charge-2")
    )

    with pytest.raises(ConflictError, match="Telegram charge"):
        await purchases.mark_paid(second.id, telegram_charge_id="charge-unique")
