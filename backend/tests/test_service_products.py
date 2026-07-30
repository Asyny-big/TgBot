"""Unit tests for ProductService."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import TelegramSettings
from app.core.exceptions import (
    ConflictError,
    InvalidDeliveryUrlError,
    InvalidPriceError,
    InvalidSlugError,
    ProductNotFoundError,
    SlugAlreadyExistsError,
)
from app.domain.commands import ProductDraft, ProductUpdate
from app.domain.pagination import PageRequest, ProductFilters
from app.services.products import ProductService
from tests.fakes import FakeUnitOfWorkFactory
from tests.settings_factory import VALID_BOT_TOKEN


def _telegram() -> TelegramSettings:
    return TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username="MyShopBot",
        use_webhook=False,
        webhook_secret=SecretStr("webhook-secret-value"),
    )


@pytest.fixture
def uow_factory() -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory()


@pytest.fixture
def service(uow_factory: FakeUnitOfWorkFactory) -> ProductService:
    return ProductService(uow_factory=uow_factory, telegram=_telegram())


def _draft(**overrides: object) -> ProductDraft:
    values: dict[str, object] = {
        "slug": "vip1",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": "https://t.me/+invite",
        "price_stars": 100,
        "price_usdt": Decimal("4.99"),
    }
    values.update(overrides)
    return ProductDraft(**values)  # type: ignore[arg-type]


async def test_create_normalises_input_and_returns_a_deep_link(service: ProductService) -> None:
    product = await service.create(_draft(slug="  vip1  ", title="  VIP access  "))

    assert product.slug == "vip1"
    assert product.title == "VIP access"
    assert service.deep_link(product.slug) == "https://t.me/MyShopBot?start=vip1"


@pytest.mark.parametrize("slug", ["vip 1", "приват", "x" * 65, ""])
async def test_create_rejects_slugs_a_deep_link_cannot_carry(
    service: ProductService,
    slug: str,
) -> None:
    with pytest.raises(InvalidSlugError):
        await service.create(_draft(slug=slug))


@pytest.mark.parametrize(
    "url",
    ["t.me/+invite", "ftp://example.com/file", "javascript:alert(1)", "https://", "  "],
)
async def test_create_rejects_a_link_that_is_not_an_absolute_http_url(
    service: ProductService,
    url: str,
) -> None:
    with pytest.raises(InvalidDeliveryUrlError):
        await service.create(_draft(delivery_url=url))


async def test_create_requires_at_least_one_price() -> None:
    with pytest.raises(InvalidPriceError):
        _draft(price_stars=None, price_usdt=None)


async def test_duplicate_slug_is_reported(service: ProductService) -> None:
    await service.create(_draft(slug="taken"))
    with pytest.raises(SlugAlreadyExistsError):
        await service.create(_draft(slug="taken"))


async def test_update_validates_new_values(service: ProductService) -> None:
    product = await service.create(_draft())

    with pytest.raises(InvalidSlugError):
        await service.update(product.id, ProductUpdate(slug="not a slug"))
    with pytest.raises(InvalidDeliveryUrlError):
        await service.update(product.id, ProductUpdate(delivery_url="mailto:me@example.com"))

    updated = await service.update(product.id, ProductUpdate(slug=" pack18 ", title=" Pack "))
    assert updated.slug == "pack18"
    assert updated.title == "Pack"


async def test_update_cannot_strip_every_price(service: ProductService) -> None:
    """A product with no price at all could never be bought again."""
    product = await service.create(_draft())

    with pytest.raises(InvalidPriceError):
        await service.update(product.id, ProductUpdate(price_stars=None, price_usdt=None))

    # Clearing only one rail is fine.
    updated = await service.update(product.id, ProductUpdate(price_usdt=None))
    assert updated.price_usdt is None
    assert updated.price_stars == 100

    # ...but then clearing the remaining one is not.
    with pytest.raises(InvalidPriceError):
        await service.update(product.id, ProductUpdate(price_stars=None))


async def test_update_of_a_missing_product_raises(service: ProductService) -> None:
    with pytest.raises(ProductNotFoundError):
        await service.update(uuid4(), ProductUpdate(title="x"))


async def test_get_and_lookup_by_slug(service: ProductService) -> None:
    product = await service.create(_draft(is_active=False))

    assert await service.get(product.id) == product
    assert await service.get_by_slug("vip1") == product
    with pytest.raises(ProductNotFoundError):
        await service.get_by_slug("vip1", only_active=True)
    with pytest.raises(ProductNotFoundError):
        await service.get(uuid4())


async def test_listing_passes_filters_through(service: ProductService) -> None:
    await service.create(_draft(slug="one", title="Course one"))
    await service.create(_draft(slug="two", title="Sticker pack", is_active=False))

    page = await service.list_products(ProductFilters(search="course"), PageRequest(limit=10))
    assert page.total == 1
    assert page.items[0].slug == "one"

    active = await service.list_products(ProductFilters(is_active=True), PageRequest())
    assert [item.slug for item in active.items] == ["one"]


async def test_delete_is_refused_for_a_sold_product(
    service: ProductService,
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    from app.domain.commands import PurchaseDraft  # noqa: PLC0415
    from app.domain.enums import Currency, PaymentProvider  # noqa: PLC0415

    product = await service.create(_draft())
    await uow_factory.unit.purchases.create(
        PurchaseDraft(
            user_id=1,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(100),
            currency=Currency.XTR,
            external_id="inv-1",
        )
    )

    with pytest.raises(ConflictError):
        await service.delete(product.id)

    other = await service.create(_draft(slug="unsold"))
    await service.delete(other.id)
    with pytest.raises(ProductNotFoundError):
        await service.get(other.id)
