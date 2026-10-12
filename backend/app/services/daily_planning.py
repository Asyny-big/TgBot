"""Daily Content: the planner — every buyer's delivery for their day.

Once a minute the worker asks: whose window is open right now, without a
delivery for its day yet? The window opens an hour before the buyer's anchor
and stays open — late, but not lost — until its deadline, so a planner that
was down for a while catches up on *today*, never on a day that is over: a
deadline always falls before the next day's window opens.

The answer is opened in batches with one ``INSERT … ON CONFLICT DO NOTHING``
per page of buyers: two planners, or the same one twice, open each day once.
The first attempt is due at the buyer's own time — the anchor — or at once
when that has already passed.

Nothing is planned while Daily Content is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger
from app.domain.commands import DailyDeliveryDraft
from app.domain.daily import DAILY_ACCESS_MILESTONE, due_window
from app.domain.enums import DailyDeliveryTrigger

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

    from app.core.config import DailySettings
    from app.domain.entities import DailySubscription
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)

PLANNER_PAGE_SIZE: Final = 200
"""Buyers looked at per transaction."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanReport:
    """What one planning pass did."""

    scanned: int = 0
    """Buyers who receive deliveries."""

    opened: int = 0
    """Deliveries opened by this pass."""


def scheduled_delivery(
    subscription: DailySubscription,
    now: datetime,
    *,
    recovery: timedelta,
) -> DailyDeliveryDraft | None:
    """The delivery this buyer is due right now, if their window is open.

    Exactly what ``DailyDeliveryService.open_due`` opens for one buyer.
    """
    if subscription.anchor_minute is None:
        return None
    window = due_window(subscription.anchor_minute, now, recovery=recovery)
    if window is None:
        return None
    return DailyDeliveryDraft(
        user_id=subscription.user_id,
        trigger=DailyDeliveryTrigger.SCHEDULE,
        window=window,
        first_attempt_at=max(now, window.center),
    )


@dataclass(frozen=True, slots=True)
class DailyPlanningService:
    """Opens each buyer's delivery for their day, once."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    clock: Callable[[], datetime] = field(default=_utc_now)
    page_size: int = PLANNER_PAGE_SIZE

    async def plan(self) -> PlanReport:
        """One pass over every buyer who receives deliveries."""
        if not self.settings.enabled:
            return PlanReport()
        now = self.clock()
        scanned = opened = 0
        after: int | None = None
        while True:
            async with self.uow_factory() as uow:
                page = await uow.daily_subscriptions.deliverable(
                    min_completed=DAILY_ACCESS_MILESTONE,
                    after_user_id=after,
                    limit=self.page_size,
                    only=self.settings.beta_user_ids,
                )
                drafts: list[DailyDeliveryDraft] = []
                for subscription in page:
                    draft = scheduled_delivery(subscription, now, recovery=self.settings.recovery)
                    if draft is not None:
                        drafts.append(draft)
                opened += await uow.daily_deliveries.open_many(drafts)
            scanned += len(page)
            if len(page) < self.page_size:
                break
            after = page[-1].user_id
        if opened:
            logger.info("daily_deliveries_planned", opened=opened, scanned=scanned)
        return PlanReport(scanned=scanned, opened=opened)


__all__ = ["PLANNER_PAGE_SIZE", "DailyPlanningService", "PlanReport", "scheduled_delivery"]
