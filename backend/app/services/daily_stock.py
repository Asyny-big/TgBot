"""Daily Content: how long the library lasts each buyer.

``total items / buyers`` says nothing: every buyer draws from their own pool —
the active items they have neither received nor hold. The snapshot counts that
pool per buyer in one query, and the domain's ``summarize_stock`` turns it into
the average, the minimum, the buyers with nothing left and the forecast: the
stock, in days, of the worst-off tenth, given how fast new items arrive.

Read-only: safe to call whether Daily Content is on or off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.domain.daily import DAILY_ACCESS_MILESTONE, remaining_content, summarize_stock

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.daily import DeliveryCounts, StockSummary
    from app.domain.uow import UnitOfWorkFactory

ADD_RATE_WINDOW: Final = timedelta(days=7)
"""New items per day are averaged over this long."""

FAILURE_WINDOW: Final = timedelta(hours=24)
"""Deliveries looked at by the failure alert."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class StockSnapshot:
    """The library against the buyers, right now."""

    summary: StockSummary | None
    """``None`` while nobody receives deliveries."""

    active_items: int
    add_rate_per_day: float


@dataclass(frozen=True, slots=True)
class DailyStockService:
    """Reads the stock and the recent delivery outcomes."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    clock: Callable[[], datetime] = field(default=_utc_now)

    async def snapshot(self) -> StockSnapshot:
        """Every receiving buyer's remaining items, summarised."""
        now = self.clock()
        async with self.uow_factory() as uow:
            active = await uow.daily_content.count_active()
            received = await uow.daily_subscriptions.received_active_counts(
                min_completed=DAILY_ACCESS_MILESTONE,
                only=self.settings.beta_user_ids,
            )
            added = await uow.daily_content.added_since(now - ADD_RATE_WINDOW)
        rate = added / ADD_RATE_WINDOW.days
        remaining = [remaining_content(active, count) for count in received.values()]
        return StockSnapshot(
            summary=summarize_stock(
                remaining,
                add_rate_per_day=rate,
                threshold_days=self.settings.low_stock_days,
            ),
            active_items=active,
            add_rate_per_day=rate,
        )

    async def recent_outcomes(self) -> DeliveryCounts:
        """Deliveries of the last day: sent, and failed for a reason that matters."""
        async with self.uow_factory() as uow:
            return await uow.daily_deliveries.outcome_counts(since=self.clock() - FAILURE_WINDOW)


__all__ = ["ADD_RATE_WINDOW", "FAILURE_WINDOW", "DailyStockService", "StockSnapshot"]
