"""Dashboard and search use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.stats import StatsOverview, StatsPeriod

if TYPE_CHECKING:
    from datetime import datetime

    from app.domain.entities import PurchaseRecord
    from app.domain.pagination import Page, PageRequest, PurchaseFilters
    from app.domain.uow import UnitOfWorkFactory

DEFAULT_TOP_PRODUCTS = 10
DEFAULT_RECENT_PURCHASES = 10


@dataclass(frozen=True, slots=True)
class StatsService:
    """Aggregates every dashboard number in one transaction."""

    uow_factory: UnitOfWorkFactory

    async def overview(
        self,
        *,
        now: datetime | None = None,
        top_limit: int = DEFAULT_TOP_PRODUCTS,
        recent_limit: int = DEFAULT_RECENT_PURCHASES,
    ) -> StatsOverview:
        """Today / week / month / all-time revenue, top products and last sales."""
        async with self.uow_factory() as uow:
            stats = uow.stats
            return StatsOverview(
                today=await stats.revenue(StatsPeriod.TODAY, now=now),
                week=await stats.revenue(StatsPeriod.WEEK, now=now),
                month=await stats.revenue(StatsPeriod.MONTH, now=now),
                total=await stats.revenue(StatsPeriod.ALL, now=now),
                top_products=await stats.top_products(
                    StatsPeriod.MONTH,
                    limit=top_limit,
                    now=now,
                ),
                recent_purchases=await stats.recent_purchases(limit=recent_limit),
                products_total=await uow.products.count(),
                products_active=await uow.products.count(only_active=True),
                users_total=await uow.users.count(),
            )

    async def search_purchases(
        self,
        filters: PurchaseFilters,
        page: PageRequest,
    ) -> Page[PurchaseRecord]:
        """Search purchases by Telegram id, username, product, invoice or charge."""
        async with self.uow_factory() as uow:
            return await uow.purchases.search(filters, page)
