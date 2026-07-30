"""Product use cases."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.core.exceptions import (
    InvalidDeliveryUrlError,
    InvalidPriceError,
    ProductNotFoundError,
)
from app.core.logging import get_logger
from app.domain.patch import is_set
from app.domain.slug import normalise_slug

if TYPE_CHECKING:
    from uuid import UUID

    from app.core.config import TelegramSettings
    from app.domain.commands import ProductDraft, ProductUpdate
    from app.domain.entities import Product
    from app.domain.pagination import Page, PageRequest, ProductFilters
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _validate_delivery_url(url: str) -> str:
    """Ensure the stored link is an absolute http(s) URL."""
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise InvalidDeliveryUrlError(url=url)
    return candidate


@dataclass(frozen=True, slots=True)
class ProductService:
    """Creates, edits and reads products; owns nothing about payments."""

    uow_factory: UnitOfWorkFactory
    telegram: TelegramSettings

    def deep_link(self, slug: str) -> str:
        """Public link that opens this product's card."""
        return self.telegram.deep_link(slug)

    async def create(self, draft: ProductDraft) -> Product:
        """Validate and store a new product.

        Raises:
            InvalidSlugError: the slug cannot be used in a deep link.
            InvalidDeliveryUrlError: the delivery link is not an absolute URL.
            SlugAlreadyExistsError: the slug is taken.
        """
        prepared = replace(
            draft,
            slug=normalise_slug(draft.slug),
            title=draft.title.strip(),
            delivery_url=_validate_delivery_url(draft.delivery_url),
        )
        async with self.uow_factory() as uow:
            product = await uow.products.create(prepared)
        logger.info("product_created", product_id=str(product.id), slug=product.slug)
        return product

    async def update(self, product_id: UUID, changes: ProductUpdate) -> Product:
        """Apply a partial update, keeping the product purchasable.

        Raises:
            ProductNotFoundError: no product with this id.
            InvalidPriceError: the update would leave the product with no price.
            InvalidSlugError, InvalidDeliveryUrlError: invalid new values.
            SlugAlreadyExistsError: the new slug is taken.
        """
        prepared = self._prepare_changes(changes)
        async with self.uow_factory() as uow:
            current = await uow.products.get(product_id)
            if current is None:
                raise ProductNotFoundError(product_id=str(product_id))
            self._ensure_still_purchasable(current, prepared)
            product = await uow.products.update(product_id, prepared)
        logger.info(
            "product_updated",
            product_id=str(product.id),
            fields=sorted(prepared.changes()),
        )
        return product

    async def delete(self, product_id: UUID) -> None:
        """Delete a product that has never been purchased.

        Raises:
            ProductNotFoundError: no product with this id.
            ConflictError: the product has purchases; deactivate it instead.
        """
        async with self.uow_factory() as uow:
            await uow.products.delete(product_id)
        logger.info("product_deleted", product_id=str(product_id))

    async def get(self, product_id: UUID) -> Product:
        """Return a product by id.

        Raises:
            ProductNotFoundError: no product with this id.
        """
        async with self.uow_factory() as uow:
            product = await uow.products.get(product_id)
        if product is None:
            raise ProductNotFoundError(product_id=str(product_id))
        return product

    async def get_by_slug(self, slug: str, *, only_active: bool = False) -> Product:
        """Return a product by deep-link slug.

        Raises:
            ProductNotFoundError: no matching product.
        """
        async with self.uow_factory() as uow:
            product = await uow.products.get_by_slug(slug, only_active=only_active)
        if product is None:
            raise ProductNotFoundError(slug=slug)
        return product

    async def list_products(self, filters: ProductFilters, page: PageRequest) -> Page[Product]:
        """Paginated product listing for the admin panel."""
        async with self.uow_factory() as uow:
            return await uow.products.list_products(filters, page)

    @staticmethod
    def _prepare_changes(changes: ProductUpdate) -> ProductUpdate:
        updates: dict[str, object] = {}
        if is_set(changes.slug):
            updates["slug"] = normalise_slug(changes.slug)
        if is_set(changes.title):
            updates["title"] = changes.title.strip()
        if is_set(changes.delivery_url):
            updates["delivery_url"] = _validate_delivery_url(changes.delivery_url)
        return replace(changes, **updates)  # type: ignore[arg-type]

    @staticmethod
    def _ensure_still_purchasable(current: Product, changes: ProductUpdate) -> None:
        """A product with no price at all could never be bought again."""
        stars = changes.price_stars if is_set(changes.price_stars) else current.price_stars
        usdt = changes.price_usdt if is_set(changes.price_usdt) else current.price_usdt
        if stars is None and usdt is None:
            raise InvalidPriceError(product_id=str(current.id))
