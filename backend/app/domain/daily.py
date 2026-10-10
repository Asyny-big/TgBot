"""Daily Content: access, personal delivery windows and content stock.

Everything here is pure — the arithmetic behind the Daily Content bot, with no
database and no Telegram in sight.

**Access** is read off the loyalty programme's own count of completed purchases
(``VIP_MILESTONE`` and ``CONTENT_SUGGESTION_MILESTONE``); there is no second
counter anywhere.

**Windows.** A buyer's first interaction while they have access is their
activation: their first item is sent right away, and that moment — as a time of
day in Europe/Moscow — becomes the centre of their personal daily window, one
hour either side of it. Buyers therefore spread over the day on their own.

**Days.** A delivery belongs to the Moscow date of its window's centre, and a
buyer gets at most one per date. A delivery that could not be sent inside its
window may still be sent late — losing a day is worse than a late item — up to
its deadline, which always falls before the next day's window opens.

**Stock.** Each buyer draws from their own pool: the active items they have not
received. ``total / users`` therefore says nothing; what matters is how many
items each active buyer has left, and since a buyer takes at most one a day,
that number is their stock in days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from app.domain.loyalty import CONTENT_SUGGESTION_MILESTONE, VIP_MILESTONE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.domain.entities import DailyContent

DAILY_TIMEZONE: Final = ZoneInfo("Europe/Moscow")
"""The one timezone of Daily Content: windows, dates and "today" are Moscow's."""

DAILY_ACCESS_MILESTONE: Final = VIP_MILESTONE
"""Completed purchases that open the Daily Content bot."""

WINDOW_HALF_WIDTH: Final = timedelta(hours=1)
"""A daily window runs this long either side of the buyer's anchor time."""

MINUTES_PER_DAY: Final = 24 * 60

MAX_RECOVERY: Final = timedelta(hours=22)
"""Latest a late delivery may still go out after the window's centre.

The next day's window opens 23 hours after this one's centre; stopping an hour
earlier keeps two deliveries from ever landing back to back.
"""


# --------------------------------------------------------------------------- #
# Access                                                                      #
# --------------------------------------------------------------------------- #


def has_daily_access(completed_purchases: int) -> bool:
    """Whether this many completed purchases open the Daily Content bot."""
    return completed_purchases >= DAILY_ACCESS_MILESTONE


def can_suggest_content(completed_purchases: int) -> bool:
    """Whether this many completed purchases unlock "💡 Suggest content"."""
    return completed_purchases >= CONTENT_SUGGESTION_MILESTONE


# --------------------------------------------------------------------------- #
# Personal windows                                                            #
# --------------------------------------------------------------------------- #


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        message = "Daily Content works with timezone-aware datetimes only"
        raise ValueError(message)


def anchor_minute_of(moment: datetime) -> int:
    """The minute of the day, in Moscow, at which ``moment`` happened."""
    _require_aware(moment)
    local = moment.astimezone(DAILY_TIMEZONE)
    return local.hour * 60 + local.minute


def moscow_date_of(moment: datetime) -> date:
    """The Moscow calendar date of ``moment``."""
    _require_aware(moment)
    return moment.astimezone(DAILY_TIMEZONE).date()


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryWindow:
    """When one day's delivery is due, and until when it may still go out.

    All moments are UTC. ``opens_at``/``closes_at`` bound the normal window;
    a send after ``closes_at`` is late but still that day's delivery, up to
    ``deadline_at``.
    """

    delivery_date: date
    center: datetime
    opens_at: datetime
    closes_at: datetime
    deadline_at: datetime

    def accepts(self, moment: datetime) -> bool:
        """Whether a send at ``moment`` still counts as this day's delivery."""
        return self.opens_at <= moment < self.deadline_at

    def is_late(self, moment: datetime) -> bool:
        """Whether a send at ``moment`` happens after the normal window."""
        return moment >= self.closes_at


def _require_recovery(recovery: timedelta) -> None:
    if not WINDOW_HALF_WIDTH <= recovery <= MAX_RECOVERY:
        message = (
            f"recovery must lie between {WINDOW_HALF_WIDTH} and {MAX_RECOVERY}, got {recovery}"
        )
        raise ValueError(message)


def _window(delivery_date: date, center: datetime, recovery: timedelta) -> DeliveryWindow:
    return DeliveryWindow(
        delivery_date=delivery_date,
        center=center,
        opens_at=center - WINDOW_HALF_WIDTH,
        closes_at=center + WINDOW_HALF_WIDTH,
        deadline_at=center + recovery,
    )


def window_for(anchor_minute: int, delivery_date: date, *, recovery: timedelta) -> DeliveryWindow:
    """The window of the delivery due on ``delivery_date`` for this anchor.

    The centre is the anchor's wall-clock time on that Moscow date; the rest
    is computed in UTC, so the window is exactly two hours long whatever the
    clocks do.
    """
    if not 0 <= anchor_minute < MINUTES_PER_DAY:
        message = f"anchor_minute must be within a day, got {anchor_minute}"
        raise ValueError(message)
    _require_recovery(recovery)
    local = datetime.combine(
        delivery_date,
        time(hour=anchor_minute // 60, minute=anchor_minute % 60),
        tzinfo=DAILY_TIMEZONE,
    )
    return _window(delivery_date, local.astimezone(UTC), recovery)


def activation_window(moment: datetime, *, recovery: timedelta) -> DeliveryWindow:
    """The window of the item sent right away at activation.

    Centred on the activation itself — it *is* that day's delivery — so the
    buyer's next item is due one day later in the window around the same time.
    """
    _require_aware(moment)
    _require_recovery(recovery)
    center = moment.astimezone(UTC)
    return _window(moscow_date_of(moment), center, recovery)


def due_window(anchor_minute: int, now: datetime, *, recovery: timedelta) -> DeliveryWindow | None:
    """The delivery this anchor is due right now, if any.

    That is the window that has opened and whose deadline has not passed. At
    most one qualifies: a deadline always falls before the next day's opening.
    """
    _require_aware(now)
    today = moscow_date_of(now)
    for delivery_date in (today - timedelta(days=1), today, today + timedelta(days=1)):
        window = window_for(anchor_minute, delivery_date, recovery=recovery)
        if window.accepts(now):
            return window
    return None


# --------------------------------------------------------------------------- #
# Retries                                                                     #
# --------------------------------------------------------------------------- #


def retry_delay(
    attempt: int,
    *,
    initial: timedelta,
    maximum: timedelta,
    jitter: float,
    retry_after: timedelta | None = None,
) -> timedelta:
    """How long to wait before attempt ``attempt + 1``.

    Exponential (doubling from ``initial``), capped at ``maximum``, spread by
    ``jitter`` (a factor in ``[0, 1)`` — tests pass it, the worker draws it) and
    never shorter than Telegram's own ``retry_after`` hint.
    """
    if attempt < 1:
        message = f"attempt counts from 1, got {attempt}"
        raise ValueError(message)
    if not 0 <= jitter < 1:
        message = f"jitter must lie in [0, 1), got {jitter}"
        raise ValueError(message)
    exponential = initial * (1 << (attempt - 1))
    delay = min(exponential, maximum)
    delay += delay * jitter / 4
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


# --------------------------------------------------------------------------- #
# Content stock                                                               #
# --------------------------------------------------------------------------- #


def remaining_content(active_items: int, received_active_items: int) -> int:
    """Active items a buyer has not received yet: their stock, in days."""
    if active_items < 0 or received_active_items < 0:
        message = "item counts cannot be negative"
        raise ValueError(message)
    return max(active_items - received_active_items, 0)


def days_of_stock(remaining: int, *, add_rate_per_day: float) -> float:
    """How many days a buyer's stock lasts while new items keep arriving.

    The buyer takes one item a day and gains ``add_rate_per_day`` new ones, so
    the stock shrinks by ``1 - rate`` a day. At a rate of one a day or more it
    never runs out: ``math.inf``.
    """
    if remaining < 0 or add_rate_per_day < 0:
        message = "stock and add rate cannot be negative"
        raise ValueError(message)
    if remaining == 0:
        return 0.0
    if add_rate_per_day >= 1:
        return math.inf
    return remaining / (1 - add_rate_per_day)


def percentile(values: Sequence[float], share: float) -> float:
    """The nearest-rank percentile: the smallest value covering ``share``.

    ``share`` of 0.1 answers "how much does the worst-off tenth of buyers
    have?" — with 14 buyers, that is the second-lowest value.
    """
    if not values:
        message = "a percentile needs at least one value"
        raise ValueError(message)
    if not 0 < share <= 1:
        message = f"share must lie in (0, 1], got {share}"
        raise ValueError(message)
    ordered = sorted(values)
    rank = max(math.ceil(share * len(ordered)), 1)
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True, kw_only=True)
class StockSummary:
    """How much Daily Content the active buyers have left."""

    active_users: int
    average_remaining: float
    minimum_remaining: int
    median_remaining: float
    exhausted_users: int
    """Buyers with nothing left: they wait for new items."""

    users_at_risk: int
    """Buyers whose stock lasts at most the threshold."""

    forecast_days: float
    """Stock of the worst-off tenth of buyers, in days (nearest rank)."""

    @property
    def is_low(self) -> bool:
        """Whether the worst-off tenth is at or under the threshold."""
        return self.users_at_risk > 0 and self.users_at_risk * 10 >= self.active_users


def summarize_stock(
    remaining: Sequence[int],
    *,
    add_rate_per_day: float,
    threshold_days: float,
) -> StockSummary | None:
    """Summarise per-buyer stock, or ``None`` when nobody is active."""
    if not remaining:
        return None
    days = [days_of_stock(value, add_rate_per_day=add_rate_per_day) for value in remaining]
    ordered = sorted(remaining)
    middle = len(ordered) // 2
    median = (
        float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return StockSummary(
        active_users=len(remaining),
        average_remaining=sum(remaining) / len(remaining),
        minimum_remaining=ordered[0],
        median_remaining=median,
        exhausted_users=sum(1 for value in remaining if value == 0),
        users_at_risk=sum(1 for value in days if value <= threshold_days),
        forecast_days=percentile(days, 0.1),
    )


# --------------------------------------------------------------------------- #
# Sending                                                                     #
# --------------------------------------------------------------------------- #


class DailyContentSender(Protocol):
    """Port: put one library item in front of a buyer.

    Implementations translate their transport's failures into the
    ``DailySend*Error`` family, saying whether Telegram *definitely* did not
    take the message (blocked, rejected) or the outcome is uncertain
    (transient).
    """

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        """Send the item; return the Telegram message id of what the buyer got.

        Raises:
            DailySendTransientError: a temporary, possibly ambiguous failure.
            DailySendBlockedError: the buyer cannot receive messages.
            DailyContentRejectedError: Telegram refused this item.
            DailySendRejectedError: Telegram refused the message otherwise.
        """
        ...


class DeliveryStep(StrEnum):
    """What one pass over a delivery did — for callers, logs and tests."""

    SENT = "sent"
    """Telegram accepted the item. The day is done."""

    ALREADY_SENT = "already_sent"
    """The day was done before: nothing was sent again."""

    RETRY_SCHEDULED = "retry_scheduled"
    """A temporary failure: the same item goes again later."""

    ITEM_RELEASED = "item_released"
    """Telegram refused the item: it was given back and another one is due."""

    FAILED = "failed"
    """Given up for the day: refused for good, or out of attempts."""

    BLOCKED = "blocked"
    """The buyer blocked the bot: the day is closed and the buyer marked."""

    NO_CONTENT = "no_content"
    """Nothing the buyer has not received yet: looked at again later."""

    CANCELLED = "cancelled"
    """The buyer paused, blocked the bot or lost access before the send."""

    LEASE_LOST = "lease_lost"
    """Reported after another worker had taken the delivery over."""

    NOT_DUE = "not_due"
    """Not attemptable now: somebody else is sending it, or it is not due yet."""

    DISABLED = "disabled"
    """Daily Content is switched off: nothing was looked at."""


__all__ = [
    "DAILY_ACCESS_MILESTONE",
    "DAILY_TIMEZONE",
    "MAX_RECOVERY",
    "MINUTES_PER_DAY",
    "WINDOW_HALF_WIDTH",
    "DailyContentSender",
    "DeliveryStep",
    "DeliveryWindow",
    "StockSummary",
    "activation_window",
    "anchor_minute_of",
    "can_suggest_content",
    "days_of_stock",
    "due_window",
    "has_daily_access",
    "moscow_date_of",
    "percentile",
    "remaining_content",
    "retry_delay",
    "summarize_stock",
    "window_for",
]
