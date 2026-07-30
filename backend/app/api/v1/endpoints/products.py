"""Product CRUD for the admin panel."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import ContainerDep, CurrentAdmin, PageDep
from app.api.schemas.common import PageMeta, PageResponse
from app.api.schemas.products import (
    ProductCreateRequest,
    ProductPatchRequest,
    ProductResponse,
)
from app.domain.entities import Product
from app.domain.pagination import Page, ProductFilters
from app.services.products import ProductService

router = APIRouter(prefix="/products", tags=["products"])


def _to_response(products: ProductService, product: Product) -> ProductResponse:
    return ProductResponse.from_domain(product, products.deep_link(product.slug))


def _to_page(products: ProductService, page: Page[Product]) -> PageResponse[ProductResponse]:
    return PageResponse[ProductResponse](
        items=[_to_response(products, product) for product in page.items],
        meta=PageMeta(
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        ),
    )


@router.get("", response_model=PageResponse[ProductResponse], summary="List products")
async def list_products(
    admin: CurrentAdmin,
    container: ContainerDep,
    page: PageDep,
    search: Annotated[str | None, Query(max_length=255)] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> PageResponse[ProductResponse]:
    """Paginated products, newest first, with search and an activity filter."""
    del admin
    result = await container.products.list_products(
        ProductFilters(search=search, is_active=is_active),
        page,
    )
    return _to_page(container.products, result)


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
async def create_product(
    payload: ProductCreateRequest,
    admin: CurrentAdmin,
    container: ContainerDep,
) -> ProductResponse:
    """Create a product and return it with its deep link.

    Raises:
        SlugAlreadyExistsError: the slug is taken.
        InvalidDeliveryUrlError: the delivery link is not an absolute http(s) URL.
    """
    del admin
    product = await container.products.create(payload.to_draft())
    return _to_response(container.products, product)


@router.get("/{product_id}", response_model=ProductResponse, summary="Read a product")
async def get_product(
    product_id: UUID,
    admin: CurrentAdmin,
    container: ContainerDep,
) -> ProductResponse:
    """Read one product.

    Raises:
        ProductNotFoundError: no product with this id.
    """
    del admin
    product = await container.products.get(product_id)
    return _to_response(container.products, product)


@router.patch("/{product_id}", response_model=ProductResponse, summary="Update a product")
async def update_product(
    product_id: UUID,
    payload: ProductPatchRequest,
    admin: CurrentAdmin,
    container: ContainerDep,
) -> ProductResponse:
    """Apply a partial update.

    Only fields present in the request body are touched; sending ``null`` for a
    nullable field clears it.

    Raises:
        ProductNotFoundError: no product with this id.
        InvalidPriceError: the update would leave the product with no price.
        SlugAlreadyExistsError: the new slug is taken.
    """
    del admin
    if payload.is_empty:
        product = await container.products.get(product_id)
        return _to_response(container.products, product)
    product = await container.products.update(product_id, payload.to_update())
    return _to_response(container.products, product)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a product that was never sold",
)
async def delete_product(
    product_id: UUID,
    admin: CurrentAdmin,
    container: ContainerDep,
) -> None:
    """Delete a product.

    Raises:
        ProductNotFoundError: no product with this id.
        ConflictError: the product has purchases; deactivate it instead.
    """
    del admin
    await container.products.delete(product_id)
