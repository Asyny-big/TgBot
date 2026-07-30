"""SQLAlchemy implementation of the product repository."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, ProductNotFoundError, SlugAlreadyExistsError
from app.domain.pagination import Page
from app.infrastructure.db.errors import LIKE_ESCAPE, like_pattern, violated_constraint
from app.infrastructure.db.mappers import to_product
from app.infrastructure.db.models import ProductModel, PurchaseModel

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import ProductDraft, ProductUpdate
    from app.domain.entities import Product
    from app.domain.pagination import PageRequest, ProductFilters


class SqlAlchemyProductRepository:
    """Product persistence backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, product_id: UUID) -> Product | None:
        model = await self._session.get(ProductModel, product_id)
        return to_product(model) if model is not None else None

    async def get_by_slug(self, slug: str, *, only_active: bool = False) -> Product | None:
        statement = select(ProductModel).where(ProductModel.slug == slug)
        if only_active:
            statement = statement.where(ProductModel.is_active.is_(True))
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_product(model) if model is not None else None

    async def list_products(self, filters: ProductFilters, page: PageRequest) -> Page[Product]:
        conditions = self._conditions(filters)

        total = await self._session.scalar(
            select(func.count()).select_from(ProductModel).where(*conditions)
        )
        statement = (
            select(ProductModel)
            .where(*conditions)
            .order_by(ProductModel.created_at.desc(), ProductModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return Page(
            items=tuple(to_product(model) for model in models),
            total=total or 0,
            limit=page.limit,
            offset=page.offset,
        )

    async def create(self, draft: ProductDraft) -> Product:
        model = ProductModel(
            slug=draft.slug,
            title=draft.title,
            description=draft.description,
            delivery_url=draft.delivery_url,
            photo_file_id=draft.photo_file_id,
            price_stars=draft.price_stars,
            price_usdt=draft.price_usdt,
            is_active=draft.is_active,
        )
        # A SAVEPOINT keeps the surrounding transaction usable when the insert
        # is rejected: the service layer, not the repository, owns the commit.
        try:
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError as error:
            if violated_constraint(error) == "uq_products_slug":
                raise SlugAlreadyExistsError(slug=draft.slug) from error
            raise
        await self._session.refresh(model)
        return to_product(model)

    async def update(self, product_id: UUID, changes: ProductUpdate) -> Product:
        model = await self._session.get(ProductModel, product_id)
        if model is None:
            raise ProductNotFoundError(product_id=str(product_id))

        applied = changes.changes()
        if not applied:
            return to_product(model)

        try:
            async with self._session.begin_nested():
                for field, value in applied.items():
                    setattr(model, field, value)
                await self._session.flush()
        except IntegrityError as error:
            if violated_constraint(error) == "uq_products_slug":
                raise SlugAlreadyExistsError(slug=str(applied.get("slug"))) from error
            raise
        await self._session.refresh(model)
        return to_product(model)

    async def delete(self, product_id: UUID) -> None:
        model = await self._session.get(ProductModel, product_id)
        if model is None:
            raise ProductNotFoundError(product_id=str(product_id))

        purchases = await self._session.scalar(
            select(func.count())
            .select_from(PurchaseModel)
            .where(PurchaseModel.product_id == product_id)
        )
        if purchases:
            message = "Product has purchases and cannot be deleted; deactivate it instead"
            raise ConflictError(message, product_id=str(product_id), purchases=purchases)

        await self._session.delete(model)
        await self._session.flush()

    async def count(self, *, only_active: bool | None = None) -> int:
        statement = select(func.count()).select_from(ProductModel)
        if only_active is not None:
            statement = statement.where(ProductModel.is_active.is_(only_active))
        return await self._session.scalar(statement) or 0

    @staticmethod
    def _conditions(filters: ProductFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.search:
            pattern = like_pattern(filters.search)
            conditions.append(
                or_(
                    ProductModel.title.ilike(pattern, escape=LIKE_ESCAPE),
                    ProductModel.slug.ilike(pattern, escape=LIKE_ESCAPE),
                    ProductModel.description.ilike(pattern, escape=LIKE_ESCAPE),
                )
            )
        if filters.is_active is not None:
            conditions.append(ProductModel.is_active.is_(filters.is_active))
        return conditions
