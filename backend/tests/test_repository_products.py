"""Integration tests for the product repository (real PostgreSQL)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, ProductNotFoundError, SlugAlreadyExistsError
from app.domain.commands import ProductUpdate
from app.domain.pagination import PageRequest, ProductFilters
from app.infrastructure.db.models import ProductModel
from tests.db import product_draft, purchase_draft, user_draft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository


async def test_create_and_read_back(products: SqlAlchemyProductRepository) -> None:
    created = await products.create(product_draft(slug="vip1", title="VIP access"))

    assert created.slug == "vip1"
    assert created.price_stars == 100
    assert created.price_usdt == Decimal("4.99")
    assert created.is_active
    assert created.created_at.tzinfo is not None

    assert await products.get(created.id) == created
    assert await products.get_by_slug("vip1") == created
    assert await products.get_by_slug("missing") is None


async def test_get_by_slug_can_require_active(products: SqlAlchemyProductRepository) -> None:
    created = await products.create(product_draft(slug="hidden", is_active=False))

    assert await products.get_by_slug("hidden") == created
    assert await products.get_by_slug("hidden", only_active=True) is None


async def test_duplicate_slug_is_rejected_and_session_survives(
    products: SqlAlchemyProductRepository,
) -> None:
    await products.create(product_draft(slug="taken"))

    with pytest.raises(SlugAlreadyExistsError) as error:
        await products.create(product_draft(slug="taken"))
    assert error.value.details["slug"] == "taken"

    # The savepoint rolled back only the failed insert: the session still works.
    survivor = await products.create(product_draft(slug="free"))
    assert survivor.slug == "free"
    assert await products.count() == 2


async def test_partial_update_touches_only_supplied_fields(
    products: SqlAlchemyProductRepository,
) -> None:
    created = await products.create(
        product_draft(slug="before", title="Old", photo_file_id="photo-1")
    )

    updated = await products.update(created.id, ProductUpdate(title="New"))
    assert updated.title == "New"
    assert updated.slug == "before"
    assert updated.photo_file_id == "photo-1"
    assert updated.updated_at >= created.updated_at


async def test_update_can_clear_nullable_fields(products: SqlAlchemyProductRepository) -> None:
    created = await products.create(product_draft(photo_file_id="photo-1", price_stars=100))

    updated = await products.update(
        created.id,
        ProductUpdate(photo_file_id=None, price_stars=None),
    )
    assert updated.photo_file_id is None
    assert updated.price_stars is None
    assert updated.price_usdt == Decimal("4.99")


async def test_empty_update_is_a_no_op(products: SqlAlchemyProductRepository) -> None:
    created = await products.create(product_draft())
    assert await products.update(created.id, ProductUpdate()) == created


async def test_update_to_taken_slug_is_rejected(products: SqlAlchemyProductRepository) -> None:
    await products.create(product_draft(slug="first"))
    second = await products.create(product_draft(slug="second"))

    with pytest.raises(SlugAlreadyExistsError):
        await products.update(second.id, ProductUpdate(slug="first"))

    assert (await products.get_by_slug("second")) is not None


async def test_update_of_missing_product_raises(products: SqlAlchemyProductRepository) -> None:
    with pytest.raises(ProductNotFoundError):
        await products.update(uuid4(), ProductUpdate(title="x"))


async def test_listing_filters_and_paginates(products: SqlAlchemyProductRepository) -> None:
    for index in range(5):
        await products.create(
            product_draft(slug=f"item{index}", title=f"Course {index}", is_active=index % 2 == 0)
        )
    await products.create(product_draft(slug="other", title="Sticker pack"))

    first_page = await products.list_products(ProductFilters(), PageRequest(limit=2))
    assert first_page.total == 6
    assert len(first_page.items) == 2
    assert first_page.has_more

    second_page = await products.list_products(ProductFilters(), PageRequest(limit=2, offset=4))
    first_slugs = {item.slug for item in first_page.items}
    second_slugs = {item.slug for item in second_page.items}
    assert not first_slugs & second_slugs

    by_title = await products.list_products(ProductFilters(search="course"), PageRequest())
    assert by_title.total == 5

    by_slug = await products.list_products(ProductFilters(search="other"), PageRequest())
    assert by_slug.total == 1

    only_active = await products.list_products(ProductFilters(is_active=True), PageRequest())
    assert all(item.is_active for item in only_active.items)
    assert only_active.total == 4


async def test_search_treats_wildcards_literally(products: SqlAlchemyProductRepository) -> None:
    await products.create(product_draft(slug="plain", title="Plain title"))
    await products.create(product_draft(slug="wild", title="100% discount"))

    escaped = await products.list_products(ProductFilters(search="100%"), PageRequest())
    assert escaped.total == 1
    assert escaped.items[0].slug == "wild"

    # A bare wildcard must not match everything.
    assert (await products.list_products(ProductFilters(search="%"), PageRequest())).total == 1


async def test_count_can_be_restricted_to_active(products: SqlAlchemyProductRepository) -> None:
    await products.create(product_draft(is_active=True))
    await products.create(product_draft(is_active=False))

    assert await products.count() == 2
    assert await products.count(only_active=True) == 1
    assert await products.count(only_active=False) == 1


async def test_delete_removes_a_product_without_purchases(
    products: SqlAlchemyProductRepository,
) -> None:
    created = await products.create(product_draft())
    await products.delete(created.id)

    assert await products.get(created.id) is None
    with pytest.raises(ProductNotFoundError):
        await products.delete(created.id)


async def test_delete_is_refused_when_purchases_exist(
    products: SqlAlchemyProductRepository,
    users: SqlAlchemyUserRepository,
    purchases: SqlAlchemyPurchaseRepository,
) -> None:
    product = await products.create(product_draft())
    buyer = await users.upsert(user_draft(1001))
    await purchases.create(purchase_draft(buyer, product))

    with pytest.raises(ConflictError) as error:
        await products.delete(product.id)
    assert error.value.details["purchases"] == 1
    assert await products.get(product.id) is not None


async def test_database_rejects_a_product_without_any_price(db_session: AsyncSession) -> None:
    """The price rule is enforced by the schema, not only by ``ProductDraft``."""
    db_session.add(
        ProductModel(
            slug="nopriceatall",
            title="No price",
            description="",
            delivery_url="https://example.com",
        )
    )
    with pytest.raises(IntegrityError, match="ck_products_price_present"):
        await db_session.flush()


async def test_database_rejects_a_malformed_slug(db_session: AsyncSession) -> None:
    db_session.add(
        ProductModel(
            slug="not a slug",
            title="Bad slug",
            description="",
            delivery_url="https://example.com",
            price_stars=10,
        )
    )
    with pytest.raises(IntegrityError, match="ck_products_slug_format"):
        await db_session.flush()


async def test_database_rejects_a_non_positive_price(db_session: AsyncSession) -> None:
    db_session.add(
        ProductModel(
            slug="freebie",
            title="Zero price",
            description="",
            delivery_url="https://example.com",
            price_stars=0,
        )
    )
    with pytest.raises(IntegrityError, match="ck_products_price_stars_positive"):
        await db_session.flush()
