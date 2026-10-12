"""Daily Content arithmetic, without a database or Telegram.

Access thresholds, personal windows (±1 hour around the activation time in
Europe/Moscow), late recovery up to a deadline, retry back-off and the per-buyer
content stock.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.daily import (
    DAILY_ACCESS_MILESTONE,
    DAILY_TIMEZONE,
    MAX_RECOVERY,
    WINDOW_HALF_WIDTH,
    activation_window,
    anchor_minute_of,
    can_suggest_content,
    days_of_stock,
    due_window,
    has_daily_access,
    moscow_date_of,
    percentile,
    remaining_content,
    retry_delay,
    summarize_stock,
    window_for,
)
from app.domain.loyalty import CONTENT_SUGGESTION_MILESTONE, VIP_MILESTONE

RECOVERY = timedelta(hours=12)


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Moscow wall-clock moment."""
    return datetime(year, month, day, hour, minute, tzinfo=DAILY_TIMEZONE)


# --------------------------------------------------------------------------- #
# Access                                                                      #
# --------------------------------------------------------------------------- #


def test_the_thresholds_are_the_loyalty_milestones() -> None:
    assert DAILY_ACCESS_MILESTONE == VIP_MILESTONE == 15
    assert CONTENT_SUGGESTION_MILESTONE == 20


@pytest.mark.parametrize(
    ("completed", "daily", "suggest"),
    [
        (0, False, False),
        (14, False, False),
        (15, True, False),
        (19, True, False),
        (20, True, True),
        (25, True, True),
    ],
)
def test_access_follows_the_completed_purchases(
    completed: int,
    daily: bool,
    suggest: bool,
) -> None:
    assert has_daily_access(completed) is daily
    assert can_suggest_content(completed) is suggest


# --------------------------------------------------------------------------- #
# Timezone and anchors                                                        #
# --------------------------------------------------------------------------- #


def test_the_timezone_is_moscow_and_loads() -> None:
    """The image must ship the zone database; this fails loudly if it does not."""
    assert str(DAILY_TIMEZONE) == "Europe/Moscow"
    assert msk(2026, 10, 10, 12).utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize(
    ("moment", "minute"),
    [
        (msk(2026, 10, 10, 12, 35), 12 * 60 + 35),
        (msk(2026, 10, 10, 0, 0), 0),
        (msk(2026, 10, 10, 23, 59), 23 * 60 + 59),
        # 09:10 UTC is 12:10 in Moscow.
        (datetime(2026, 10, 10, 9, 10, tzinfo=UTC), 12 * 60 + 10),
    ],
)
def test_the_anchor_is_the_moscow_minute_of_the_day(moment: datetime, minute: int) -> None:
    assert anchor_minute_of(moment) == minute


def test_the_moscow_date_can_differ_from_the_utc_date() -> None:
    late_utc = datetime(2026, 10, 10, 22, 30, tzinfo=UTC)  # 01:30 on the 11th in Moscow
    assert moscow_date_of(late_utc) == date(2026, 10, 11)


def test_naive_datetimes_are_refused() -> None:
    naive = datetime(2026, 10, 10, 12, 0)  # noqa: DTZ001 — the point of the test
    with pytest.raises(ValueError, match="timezone-aware"):
        anchor_minute_of(naive)


# --------------------------------------------------------------------------- #
# Windows                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("anchor", "opens", "closes"),
    [
        # The three examples agreed with the owner.
        ((12, 35), (11, 35), (13, 35)),
        ((9, 10), (8, 10), (10, 10)),
        ((21, 40), (20, 40), (22, 40)),
    ],
)
def test_the_window_runs_an_hour_either_side_of_the_anchor(
    anchor: tuple[int, int],
    opens: tuple[int, int],
    closes: tuple[int, int],
) -> None:
    window = window_for(anchor[0] * 60 + anchor[1], date(2026, 10, 11), recovery=RECOVERY)

    assert window.delivery_date == date(2026, 10, 11)
    assert window.center == msk(2026, 10, 11, *anchor).astimezone(UTC)
    assert window.opens_at == msk(2026, 10, 11, *opens).astimezone(UTC)
    assert window.closes_at == msk(2026, 10, 11, *closes).astimezone(UTC)
    assert window.closes_at - window.opens_at == 2 * WINDOW_HALF_WIDTH
    assert window.deadline_at == window.center + RECOVERY


def test_a_window_near_midnight_crosses_into_the_next_day() -> None:
    window = window_for(23 * 60 + 40, date(2026, 10, 10), recovery=RECOVERY)

    assert window.delivery_date == date(2026, 10, 10)
    assert window.opens_at == msk(2026, 10, 10, 22, 40).astimezone(UTC)
    assert window.closes_at == msk(2026, 10, 11, 0, 40).astimezone(UTC)


def test_a_late_send_still_counts_until_the_deadline() -> None:
    window = window_for(12 * 60, date(2026, 10, 10), recovery=RECOVERY)

    assert not window.accepts(msk(2026, 10, 10, 10, 59))
    assert window.accepts(msk(2026, 10, 10, 11, 0))
    assert not window.is_late(msk(2026, 10, 10, 12, 59))
    assert window.is_late(msk(2026, 10, 10, 13, 0))
    assert window.accepts(msk(2026, 10, 10, 23, 59))
    assert not window.accepts(msk(2026, 10, 11, 0, 0))


@pytest.mark.parametrize("hours", [0, 23, 24])
def test_the_recovery_must_end_before_the_next_window(hours: int) -> None:
    with pytest.raises(ValueError, match="recovery"):
        window_for(600, date(2026, 10, 10), recovery=timedelta(hours=hours))


def test_the_longest_recovery_never_reaches_the_next_window() -> None:
    today = window_for(600, date(2026, 10, 10), recovery=MAX_RECOVERY)
    tomorrow = window_for(600, date(2026, 10, 11), recovery=MAX_RECOVERY)
    assert today.deadline_at < tomorrow.opens_at


@pytest.mark.parametrize("anchor", [-1, 1440])
def test_an_anchor_outside_the_day_is_refused(anchor: int) -> None:
    with pytest.raises(ValueError, match="anchor_minute"):
        window_for(anchor, date(2026, 10, 10), recovery=RECOVERY)


def test_the_activation_item_is_that_days_delivery() -> None:
    """First contact at 12:35: the item goes now, tomorrow's window is 11:35-13:35."""
    activated = msk(2026, 10, 10, 12, 35)

    first = activation_window(activated, recovery=RECOVERY)
    anchor = anchor_minute_of(activated)
    following = window_for(anchor, first.delivery_date + timedelta(days=1), recovery=RECOVERY)

    assert first.delivery_date == date(2026, 10, 10)
    assert first.center == activated.astimezone(UTC)
    assert first.accepts(activated)
    assert following.opens_at == msk(2026, 10, 11, 11, 35).astimezone(UTC)
    assert following.closes_at == msk(2026, 10, 11, 13, 35).astimezone(UTC)
    # Never two deliveries back to back: the first one's deadline comes first.
    assert first.deadline_at < following.opens_at


def test_an_activation_just_after_midnight_belongs_to_the_new_date() -> None:
    first = activation_window(msk(2026, 10, 11, 0, 10), recovery=RECOVERY)
    assert first.delivery_date == date(2026, 10, 11)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (msk(2026, 10, 10, 11, 0), None),  # before the window opens
        (msk(2026, 10, 10, 11, 35), date(2026, 10, 10)),  # it just opened
        (msk(2026, 10, 10, 13, 0), date(2026, 10, 10)),  # inside it
        (msk(2026, 10, 10, 20, 0), date(2026, 10, 10)),  # late, before the deadline
        (msk(2026, 10, 11, 0, 35), None),  # past the deadline, before the next
        (msk(2026, 10, 11, 11, 40), date(2026, 10, 11)),  # the next day's window
    ],
)
def test_the_due_window_is_the_open_one_before_its_deadline(
    now: datetime,
    expected: date | None,
) -> None:
    window = due_window(12 * 60 + 35, now, recovery=RECOVERY)
    assert (window.delivery_date if window is not None else None) == expected


def test_a_late_window_crossing_midnight_is_still_due_the_next_morning() -> None:
    window = due_window(23 * 60 + 30, msk(2026, 10, 11, 1, 0), recovery=RECOVERY)
    assert window is not None
    assert window.delivery_date == date(2026, 10, 10)


# --------------------------------------------------------------------------- #
# Retries                                                                     #
# --------------------------------------------------------------------------- #


def test_retries_back_off_exponentially_up_to_a_cap() -> None:
    delays = [
        retry_delay(
            attempt,
            initial=timedelta(seconds=30),
            maximum=timedelta(minutes=10),
            jitter=0.0,
        )
        for attempt in range(1, 8)
    ]
    assert delays[:5] == [timedelta(seconds=seconds) for seconds in (30, 60, 120, 240, 480)]
    assert delays[5:] == [timedelta(minutes=10)] * 2


def test_jitter_only_lengthens_and_retry_after_is_a_floor() -> None:
    base = {"initial": timedelta(seconds=30), "maximum": timedelta(minutes=10)}
    assert retry_delay(1, jitter=0.99, **base) <= timedelta(seconds=30) * 1.25
    assert retry_delay(1, jitter=0.5, **base) > timedelta(seconds=30)
    assert retry_delay(
        1,
        jitter=0.0,
        retry_after=timedelta(seconds=90),
        **base,
    ) == timedelta(seconds=90)


@pytest.mark.parametrize(("attempt", "jitter"), [(0, 0.0), (1, 1.0), (1, -0.1)])
def test_retry_delay_refuses_nonsense(attempt: int, jitter: float) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — either message is fine
        retry_delay(
            attempt,
            initial=timedelta(seconds=1),
            maximum=timedelta(seconds=10),
            jitter=jitter,
        )


# --------------------------------------------------------------------------- #
# Stock                                                                       #
# --------------------------------------------------------------------------- #


def test_the_owners_example_100_plus_20() -> None:
    """100 items; A received 80, B received 10; 20 more are added."""
    assert remaining_content(100, 80) == 20
    assert remaining_content(100, 10) == 90
    assert remaining_content(120, 80) == 40
    assert remaining_content(120, 10) == 110


def test_remaining_never_goes_negative() -> None:
    # Items received and archived since no longer count as active ones.
    assert remaining_content(5, 7) == 0


def test_stock_in_days_accounts_for_new_items() -> None:
    assert days_of_stock(0, add_rate_per_day=0.5) == 0.0
    assert days_of_stock(10, add_rate_per_day=0.0) == 10.0
    assert days_of_stock(10, add_rate_per_day=0.5) == 20.0
    assert days_of_stock(10, add_rate_per_day=1.0) == math.inf


def test_the_percentile_is_nearest_rank() -> None:
    fourteen = list(range(14))
    assert percentile(fourteen, 0.1) == 1  # the second lowest of 14
    assert percentile([7], 0.1) == 7
    assert percentile([3, 1, 2], 1.0) == 3
    with pytest.raises(ValueError):  # noqa: PT011
        percentile([], 0.5)


def test_the_stock_summary() -> None:
    summary = summarize_stock(
        [0, 2, 3, 10, 18, 40],
        add_rate_per_day=0.0,
        threshold_days=3,
    )
    assert summary is not None
    assert summary.active_users == 6
    assert summary.minimum_remaining == 0
    assert summary.average_remaining == pytest.approx(73 / 6)
    assert summary.median_remaining == 6.5
    assert summary.exhausted_users == 1
    assert summary.users_at_risk == 3
    assert summary.forecast_days == 0.0
    assert summary.is_low


def test_one_long_time_buyer_alone_does_not_make_the_stock_low() -> None:
    """With 14 active buyers, one at risk is under the worst-off tenth."""
    remaining = [1, *([30] * 13)]
    summary = summarize_stock(remaining, add_rate_per_day=0.0, threshold_days=3)
    assert summary is not None
    assert summary.users_at_risk == 1
    assert not summary.is_low
    two_at_risk = summarize_stock([1, 2, *([30] * 12)], add_rate_per_day=0.0, threshold_days=3)
    assert two_at_risk is not None
    assert two_at_risk.is_low


def test_nobody_active_has_no_summary() -> None:
    assert summarize_stock([], add_rate_per_day=0.0, threshold_days=3) is None
