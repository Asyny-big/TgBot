"""The owner's Daily Content alerts — pure rules, no database, no Telegram.

An alert is a *state*, not a stream of messages. Each evaluation observes
something — the stock forecast, the failure rate, whether Telegram still
accepts the bot's token — and says whether the alert's condition clearly
holds, clearly does not, or sits in between. ``decide_alert`` turns that into
at most one notification:

* ``FIRE`` when the alert starts firing;
* ``REMIND`` while it keeps firing, at most once per cooldown;
* ``RESOLVE`` when it stops.

The band between "clearly holds" and "clearly does not" is the hysteresis: a
value hovering around a threshold neither fires nor resolves on every
evaluation, so the owner is never spammed with the same news.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from app.domain.enums import DailyAlertState

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from app.domain.daily import StockSummary

LOW_STOCK_HYSTERESIS_DAYS: Final = 1.0
"""Low stock resolves only once the forecast is a day above the threshold."""

FAILURE_ALERT_MIN: Final = 5
"""Fewer failed deliveries than this in a day are never an alert."""

FAILURE_ALERT_SHARE: Final = 0.2
"""Failing at least this share of a day's deliveries fires the alert..."""

FAILURE_RESOLVE_SHARE: Final = 0.1
"""...and it resolves only once the share is back under this."""


class AlertNotice(StrEnum):
    """What the owner is told about an alert."""

    FIRE = "fire"
    REMIND = "remind"
    RESOLVE = "resolve"


@dataclass(frozen=True, slots=True)
class AlertObservation:
    """One evaluation of an alert's condition.

    ``firing``: ``True`` — it clearly holds; ``False`` — it clearly does not;
    ``None`` — in between, or unknown: the alert keeps its state.
    """

    firing: bool | None
    value: dict[str, object] = field(default_factory=dict)
    """What was observed, for the record and for the message."""


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """The alert's next state, and the notification it calls for, if any."""

    state: DailyAlertState
    notice: AlertNotice | None


def decide_alert(
    current: DailyAlertState,
    *,
    notified_at: datetime | None,
    observation: AlertObservation,
    now: datetime,
    cooldown: timedelta,
) -> AlertDecision:
    """The transition an observation causes — one notification at most."""
    if observation.firing is True:
        if current is DailyAlertState.OK:
            return AlertDecision(DailyAlertState.FIRING, AlertNotice.FIRE)
        if notified_at is None or now - notified_at >= cooldown:
            return AlertDecision(DailyAlertState.FIRING, AlertNotice.REMIND)
        return AlertDecision(DailyAlertState.FIRING, None)
    if observation.firing is False and current is DailyAlertState.FIRING:
        return AlertDecision(DailyAlertState.OK, AlertNotice.RESOLVE)
    return AlertDecision(current, None)


# --------------------------------------------------------------------------- #
# Observations                                                                #
# --------------------------------------------------------------------------- #


def low_stock_observation(
    summary: StockSummary | None,
    *,
    active_items: int,
    threshold_days: float,
) -> AlertObservation:
    """Fires when the worst-off tenth of buyers has stock for ``threshold_days`` or less."""
    if summary is None:
        # Nobody receives deliveries: there is no stock to run out of.
        return AlertObservation(
            firing=False,
            value={"active_items": active_items, "threshold_days": threshold_days},
        )
    forecast = summary.forecast_days
    value: dict[str, object] = {
        "forecast_days": _finite(forecast),
        "threshold_days": threshold_days,
        "users_at_risk": summary.users_at_risk,
        "active_users": summary.active_users,
        "active_items": active_items,
    }
    if forecast <= threshold_days:
        return AlertObservation(firing=True, value=value)
    if forecast > threshold_days + LOW_STOCK_HYSTERESIS_DAYS:
        return AlertObservation(firing=False, value=value)
    return AlertObservation(firing=None, value=value)


def exhausted_observation(summary: StockSummary | None) -> AlertObservation:
    """Fires while any active buyer has received everything there is."""
    if summary is None:
        return AlertObservation(firing=False, value={"exhausted_users": 0})
    exhausted = summary.exhausted_users
    return AlertObservation(
        firing=exhausted > 0,
        value={"exhausted_users": exhausted, "active_users": summary.active_users},
    )


def failures_observation(*, sent: int, failed: int) -> AlertObservation:
    """Fires when a day's failed deliveries are many and a large share."""
    total = sent + failed
    share = failed / total if total else 0.0
    value: dict[str, object] = {
        "sent": sent,
        "failed": failed,
        "total": total,
        "percent": round(share * 100),
    }
    if failed >= FAILURE_ALERT_MIN and share >= FAILURE_ALERT_SHARE:
        return AlertObservation(firing=True, value=value)
    if failed < FAILURE_ALERT_MIN or share < FAILURE_RESOLVE_SHARE:
        return AlertObservation(firing=False, value=value)
    return AlertObservation(firing=None, value=value)


# --------------------------------------------------------------------------- #
# The bot's token                                                             #
# --------------------------------------------------------------------------- #


class TokenState(StrEnum):
    """What Telegram says about the Daily bot's token."""

    OK = "ok"
    UNAUTHORIZED = "unauthorized"
    """401: the token was revoked or replaced. Nothing can be sent."""

    UNKNOWN = "unknown"
    """No answer — the network, Telegram's servers. Says nothing either way."""


class TokenProbe(Protocol):
    """Port: ask Telegram whether it still accepts the Daily bot's token."""

    async def check(self) -> TokenState:
        """Never raises: an unclear answer is ``UNKNOWN``."""
        ...


def token_observation(state: TokenState) -> AlertObservation:
    """Fires while Telegram refuses the Daily bot's token."""
    firing = {TokenState.OK: False, TokenState.UNAUTHORIZED: True}.get(state)
    return AlertObservation(firing=firing, value={"token": state.value})


def _finite(value: float) -> float | None:
    """A JSON-safe number: an endless stock is ``None``, not ``inf``."""
    return round(value, 1) if math.isfinite(value) else None


__all__ = [
    "FAILURE_ALERT_MIN",
    "FAILURE_ALERT_SHARE",
    "FAILURE_RESOLVE_SHARE",
    "LOW_STOCK_HYSTERESIS_DAYS",
    "AlertDecision",
    "AlertNotice",
    "AlertObservation",
    "TokenProbe",
    "TokenState",
    "decide_alert",
    "exhausted_observation",
    "failures_observation",
    "low_stock_observation",
    "token_observation",
]
