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


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_token_secret_or_url_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
) -> None:
    """``DAILY_BOT_TOKEN=`` copied empty from .env.example: every process still starts.

    The shop bot, the API and the migrations load these settings too; only the
    Daily processes refuse to start, and they say why.
    """
    for name in (
        "DAILY_BOT_TOKEN",
        "DAILY_BOT_USERNAME",
        "DAILY_WEBHOOK_BASE_URL",
        "DAILY_WEBHOOK_SECRET",
    ):
        monkeypatch.setenv(name, blank)
    monkeypatch.delenv("DAILY_USE_WEBHOOK", raising=False)

    settings = DailySettings()

    assert settings.bot_token is None
    assert settings.bot_username is None
    assert settings.webhook_base_url is None
    assert settings.webhook_secret is None
    assert settings.bot_problems() == (
        "DAILY_BOT_TOKEN is not set",
        "DAILY_BOT_USERNAME is not set",
        "DAILY_WEBHOOK_BASE_URL is required in webhook mode",
        "DAILY_WEBHOOK_SECRET is required in webhook mode",
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [("DAILY_BOT_TOKEN", "not-a-token"), ("DAILY_WEBHOOK_SECRET", "too-short")],
)
def test_a_filled_in_but_wrong_value_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        DailySettings()
