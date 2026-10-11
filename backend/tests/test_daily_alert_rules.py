"""The owner's alerts as pure rules: transitions, hysteresis, cooldown."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.daily_bot import texts
from app.domain.daily import StockSummary
from app.domain.daily_alerts import (
    FAILURE_ALERT_MIN,
    AlertNotice,
    AlertObservation,
    TokenState,
    decide_alert,
    exhausted_observation,
    failures_observation,
    low_stock_observation,
    token_observation,
)
from app.domain.enums import DailyAlertKey, DailyAlertState

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
COOLDOWN = timedelta(hours=24)
OK = DailyAlertState.OK
FIRING = DailyAlertState.FIRING


def _summary(*, forecast: float, exhausted: int = 0, active: int = 10) -> StockSummary:
    return StockSummary(
        active_users=active,
        average_remaining=forecast,
        minimum_remaining=0,
        median_remaining=forecast,
        exhausted_users=exhausted,
        users_at_risk=active,
        forecast_days=forecast,
    )


def _decide(
    state: DailyAlertState,
    firing: bool | None,
    *,
    notified_at: datetime | None = None,
) -> tuple[DailyAlertState, AlertNotice | None]:
    decision = decide_alert(
        state,
        notified_at=notified_at,
        observation=AlertObservation(firing=firing),
        now=NOW,
        cooldown=COOLDOWN,
    )
    return decision.state, decision.notice


# --------------------------------------------------------------------------- #
# Transitions                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "firing", "notified_at", "expected"),
    [
        (OK, True, None, (FIRING, AlertNotice.FIRE)),
        (OK, False, None, (OK, None)),
        (OK, None, None, (OK, None)),
        (FIRING, True, NOW - timedelta(hours=1), (FIRING, None)),
        (FIRING, True, NOW - COOLDOWN, (FIRING, AlertNotice.REMIND)),
        (FIRING, True, None, (FIRING, AlertNotice.REMIND)),
        (FIRING, None, NOW - timedelta(days=3), (FIRING, None)),
        (FIRING, False, NOW - timedelta(minutes=1), (OK, AlertNotice.RESOLVE)),
    ],
    ids=[
        "starts-firing",
        "stays-quiet",
        "unknown-stays-quiet",
        "no-reminder-within-cooldown",
        "reminds-after-cooldown",
        "reminds-when-never-notified",
        "in-the-band-keeps-firing-silently",
        "resolves",
    ],
)
def test_one_notification_per_transition(
    state: DailyAlertState,
    firing: bool | None,
    notified_at: datetime | None,
    expected: tuple[DailyAlertState, AlertNotice | None],
) -> None:
    assert _decide(state, firing, notified_at=notified_at) == expected


def test_a_value_hovering_at_the_threshold_never_flaps() -> None:
    """Below, inside the band, above the threshold, inside again: one fire only."""
    threshold = 3.0
    state, notified = OK, None
    notices = []
    for forecast in (2.5, 3.5, 2.9, 3.8, 3.1, 2.0):
        observation = low_stock_observation(
            _summary(forecast=forecast),
            active_items=5,
            threshold_days=threshold,
        )
        decision = decide_alert(
            state,
            notified_at=notified,
            observation=observation,
            now=NOW,
            cooldown=COOLDOWN,
        )
        if decision.notice is not None:
            notices.append(decision.notice)
            notified = NOW
        state = decision.state
    assert notices == [AlertNotice.FIRE]
    assert state is FIRING


# --------------------------------------------------------------------------- #
# Observations                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("forecast", "firing"),
    [(3.0, True), (1.0, True), (3.5, None), (4.0, None), (4.1, False), (math.inf, False)],
)
def test_low_stock_has_a_band_of_one_day(forecast: float, firing: bool | None) -> None:
    observation = low_stock_observation(
        _summary(forecast=forecast),
        active_items=5,
        threshold_days=3.0,
    )

    assert observation.firing is firing
    assert observation.value["threshold_days"] == 3.0
    if math.isinf(forecast):
        assert observation.value["forecast_days"] is None  # JSON-safe


def test_without_buyers_there_is_no_low_stock() -> None:
    observation = low_stock_observation(None, active_items=0, threshold_days=3.0)

    assert observation.firing is False


@pytest.mark.parametrize(("exhausted", "firing"), [(0, False), (1, True), (7, True)])
def test_exhausted_fires_while_anybody_has_nothing_left(exhausted: int, firing: bool) -> None:
    assert exhausted_observation(_summary(forecast=1, exhausted=exhausted)).firing is firing
    assert exhausted_observation(None).firing is False


@pytest.mark.parametrize(
    ("sent", "failed", "firing"),
    [
        (0, FAILURE_ALERT_MIN - 1, False),  # too few to matter, however bad
        (20, FAILURE_ALERT_MIN, True),  # exactly 20 %
        (100, 6, False),  # under 10 %
        (40, 6, None),  # 13 %: inside the band
        (0, 0, False),
    ],
)
def test_failures_need_both_a_count_and_a_share(
    sent: int, failed: int, firing: bool | None
) -> None:
    observation = failures_observation(sent=sent, failed=failed)

    assert observation.firing is firing
    assert observation.value["total"] == sent + failed


@pytest.mark.parametrize(
    ("state", "firing"),
    [(TokenState.OK, False), (TokenState.UNAUTHORIZED, True), (TokenState.UNKNOWN, None)],
)
def test_the_token_alert_ignores_an_unclear_answer(state: TokenState, firing: bool | None) -> None:
    assert token_observation(state).firing is firing


# --------------------------------------------------------------------------- #
# What the owner reads                                                        #
# --------------------------------------------------------------------------- #


def test_every_alert_has_a_fire_and_a_resolve_text() -> None:
    value = {
        "forecast_days": 2.5,
        "threshold_days": 3.0,
        "active_items": 4,
        "exhausted_users": 2,
        "sent": 10,
        "failed": 5,
        "total": 15,
        "percent": 33,
    }
    for key in DailyAlertKey:
        fired = texts.alert(key, AlertNotice.FIRE, value)
        reminded = texts.alert(key, AlertNotice.REMIND, value)
        resolved = texts.alert(key, AlertNotice.RESOLVE, value)
        assert fired
        assert reminded.endswith(fired)
        assert reminded != fired
        assert resolved != fired
        assert "<" not in fired  # the bots send HTML: nothing here needs escaping


def test_the_alert_texts_carry_the_numbers() -> None:
    low = texts.alert(
        DailyAlertKey.LOW_STOCK,
        AlertNotice.FIRE,
        {"forecast_days": 2.5, "threshold_days": 3.0, "active_items": 4},
    )
    failures = texts.alert(
        DailyAlertKey.DELIVERY_FAILURES,
        AlertNotice.FIRE,
        {"failed": 5, "total": 15, "percent": 33},
    )

    assert "2.5" in low
    assert "4" in low
    assert "5" in failures
    assert "15" in failures
    assert "33%" in failures
