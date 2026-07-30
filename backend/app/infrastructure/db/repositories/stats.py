"""Dashboard aggregates.

Only ``paid`` and ``delivered`` purchases count as revenue: refunded and expired
ones are deliberately excluded so the numbers match the money actually held.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Numeric, case, func, select

from app.domain.enums import ACCESS_GRANTING_STATUSES, Currency
from app.domain.stats import RevenueSummary, TopProduct
from app.infrastructure.db.mappers import to_record
from app.infrastructure.db.models import ProductModel, PurchaseModel, UserModel

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import PurchaseRecord
    from app.domain.stats import StatsPeriod

_ZERO = Decimal("0")


def _amount_in(currency: Currency) -> ColumnElement[Decimal]:
    """Sum of amounts charged in one currency, zero when there are no rows."""
    return func.coalesce(
        func.sum(
            case(
                (PurchaseModel.currency == currency, PurchaseModel.amount),
                else_=0,
            )
        ),
        0,
    ).cast(Numeric(18, 6))


class SqlAlchemyStatsRepository:
    """Read-only aggregate queries for the admin dashboard."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def revenue(self, period: StatsPeriod, *, now: datetime | None = None) -> RevenueSummary:
        statement = select(
            func.count(PurchaseModel.id),
            _amount_in(Currency.XTR),
            _amount_in(Currency.USDT),
        ).where(*self._window(period, now))

        count, stars, usdt = (await self._session.execute(statement)).one()
        return RevenueSummary(
            period=period,
            purchases_count=int(count or 0),
            stars_amount=int(stars or _ZERO),
            usdt_amount=Decimal(usdt or _ZERO),
        )

    async def top_products(
        self,
        period: StatsPeriod,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[TopProduct, ...]:
        purchases_count = func.count(PurchaseModel.id).label("purchases_count")
        stars = _amount_in(Currency.XTR).label("stars_amount")
        usdt = _amount_in(Currency.USDT).label("usdt_amount")

        statement = (
            select(
                ProductModel.id,
                ProductModel.slug,
                ProductModel.title,
                purchases_count,
                stars,
                usdt,
            )
            .join(PurchaseModel, PurchaseModel.product_id == ProductModel.id)
            .where(*self._window(period, now))
            .group_by(ProductModel.id, ProductModel.slug, ProductModel.title)
            .order_by(purchases_count.desc(), ProductModel.title.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            TopProduct(
                product_id=row.id,
                slug=row.slug,
                title=row.title,
                purchases_count=int(row.purchases_count),
                stars_amount=int(row.stars_amount or _ZERO),
                usdt_amount=Decimal(row.usdt_amount or _ZERO),
            )
            for row in rows
        )

    async def recent_purchases(self, *, limit: int = 10) -> tuple[PurchaseRecord, ...]:
        statement = (
            select(PurchaseModel, UserModel, ProductModel)
            .join(UserModel, PurchaseModel.user_id == UserModel.telegram_id)
            .join(ProductModel, PurchaseModel.product_id == ProductModel.id)
            .where(PurchaseModel.status.in_(ACCESS_GRANTING_STATUSES))
            .order_by(PurchaseModel.paid_at.desc().nullslast(), PurchaseModel.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(to_record(purchase, user, product) for purchase, user, product in rows)

    @staticmethod
    def _window(period: StatsPeriod, now: datetime | None) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [PurchaseModel.status.in_(ACCESS_GRANTING_STATUSES)]
        start = period.start(now)
        if start is not None:
            conditions.append(PurchaseModel.paid_at >= start)
        return conditions
