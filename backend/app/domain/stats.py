"""Statistics value objects and period arithmetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import PurchaseRecord

WEEK_DAYS: Final = 7
MONTH_DAYS: Final = 30


class StatsPeriod(StrEnum):
    """Reporting windows offered by the admin dashboard.

    Windows are day aligned in UTC: ``TODAY`` starts at midnight today,
    ``WEEK`` covers the last 7 days including today, ``MONTH`` the last 30.
    """

    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"

    def start(self, now: datetime | None = None) -> datetime | None:
        """Inclusive lower bound of the window, or ``None`` for all time."""
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        match self:
            case StatsPeriod.TODAY:
                return midnight
            case StatsPeriod.WEEK:
                return midnight - timedelta(days=WEEK_DAYS - 1)
            case StatsPeriod.MONTH:
                return midnight - timedelta(days=MONTH_DAYS - 1)
            case StatsPeriod.ALL:
                return None


@dataclass(frozen=True, slots=True, kw_only=True)
class RevenueSummary:
    """Money and volume collected inside one reporting window."""

    period: StatsPeriod
    purchases_count: int = 0
    stars_amount: int = 0
    usdt_amount: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True, kw_only=True)
class TopProduct:
    """Sales aggregated for a single product."""

    product_id: UUID
    slug: str
    title: str
    purchases_count: int
    stars_amount: int
    usdt_amount: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class StatsOverview:
    """Everything the dashboard renders in one payload."""

    today: RevenueSummary
    week: RevenueSummary
    month: RevenueSummary
    total: RevenueSummary
    top_products: tuple[TopProduct, ...] = field(default_factory=tuple)
    recent_purchases: tuple[PurchaseRecord, ...] = field(default_factory=tuple)
    products_total: int = 0
    products_active: int = 0
    users_total: int = 0
