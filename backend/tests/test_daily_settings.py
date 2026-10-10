"""DAILY_* settings: off by default, ids parsed from a comma list, sane bounds."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.config import DailySettings


def test_daily_content_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DAILY_ENABLED", "DAILY_BETA_USER_IDS", "DAILY_ADMIN_TELEGRAM_IDS"):
        monkeypatch.delenv(name, raising=False)
    settings = DailySettings()
    assert settings.enabled is False
    assert settings.beta_user_ids == ()
    assert settings.admin_telegram_ids == ()
    assert settings.recovery == timedelta(hours=12)


def test_telegram_ids_come_from_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_ENABLED", "true")
    monkeypatch.setenv("DAILY_BETA_USER_IDS", "101, 202,303")
    monkeypatch.setenv("DAILY_ADMIN_TELEGRAM_IDS", "7")
    settings = DailySettings()
    assert settings.enabled is True
    assert settings.beta_user_ids == (101, 202, 303)
    assert settings.admin_telegram_ids == (7,)


@pytest.mark.parametrize("raw", ["0", "-5", "12,abc"])
def test_invalid_ids_are_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DAILY_ADMIN_TELEGRAM_IDS", raw)
    with pytest.raises((ValidationError, ValueError)):
        DailySettings()


@pytest.mark.parametrize(
    "overrides",
    [
        {"recovery_hours": 0},
        {"recovery_hours": 23},
        {"lease_seconds": 30},
        {"max_attempts": 0},
    ],
)
def test_out_of_range_values_are_refused(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        DailySettings(**overrides)  # type: ignore[arg-type]
