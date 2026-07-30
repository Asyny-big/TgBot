"""Dashboard schemas."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from app.api.schemas.common import ApiModel
from app.api.schemas.purchases import PurchaseRecordResponse
from app.domain.stats import StatsPeriod

if TYPE_CHECKING:
    from app.domain.stats import RevenueSummary, StatsOverview, TopProduct


class RevenueResponse(ApiModel):
    """Revenue inside one reporting window."""

    period: StatsPeriod
    purchases_count: int
    stars_amount: int
    usdt_amount: Decimal

    @classmethod
    def from_domain(cls, summary: RevenueSummary) -> RevenueResponse:
        return cls(
            period=summary.period,
            purchases_count=summary.purchases_count,
            stars_amount=summary.stars_amount,
            usdt_amount=summary.usdt_amount,
        )


class TopProductResponse(ApiModel):
    """One row of the best-sellers table."""

    product_id: UUID
    slug: str
    title: str
    purchases_count: int
    stars_amount: int
    usdt_amount: Decimal

    @classmethod
    def from_domain(cls, product: TopProduct) -> TopProductResponse:
        return cls(
            product_id=product.product_id,
            slug=product.slug,
            title=product.title,
            purchases_count=product.purchases_count,
            stars_amount=product.stars_amount,
            usdt_amount=product.usdt_amount,
        )


class OverviewResponse(ApiModel):
    """Everything the dashboard renders."""

    today: RevenueResponse
    week: RevenueResponse
    month: RevenueResponse
    total: RevenueResponse
    top_products: list[TopProductResponse]
    recent_purchases: list[PurchaseRecordResponse]
    products_total: int
    products_active: int
    users_total: int

    @classmethod
    def from_domain(cls, overview: StatsOverview) -> OverviewResponse:
        return cls(
            today=RevenueResponse.from_domain(overview.today),
            week=RevenueResponse.from_domain(overview.week),
            month=RevenueResponse.from_domain(overview.month),
            total=RevenueResponse.from_domain(overview.total),
            top_products=[TopProductResponse.from_domain(item) for item in overview.top_products],
            recent_purchases=[
                PurchaseRecordResponse.from_domain(record) for record in overview.recent_purchases
            ],
            products_total=overview.products_total,
            products_active=overview.products_active,
            users_total=overview.users_total,
        )
