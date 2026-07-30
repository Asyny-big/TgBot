"""Deterministic settings factory shared by every test module.

Lives outside ``conftest`` so harnesses can import it without a circular import.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.core.config import (
    AppSettings,
    BotSettings,
    CryptoBotSettings,
    DeliverySettings,
    Environment,
    LogFormat,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)

VALID_BOT_TOKEN = "123456789:AAHfake-Test-Token_for_unit_tests_only01"  # noqa: S105


def build_settings(**overrides: Any) -> Settings:
    """Return a valid settings object; ``overrides`` replaces whole groups."""
    groups: dict[str, Any] = {
        "app": AppSettings(
            environment=Environment.TESTING,
            debug=False,
            log_level="INFO",
            log_format=LogFormat.CONSOLE,
            docs_enabled=True,
        ),
        "postgres": PostgresSettings(
            host="localhost",
            user="shop",
            password=SecretStr("shop-password"),
            db="shop",
        ),
        "redis": RedisSettings(host="localhost"),
        "telegram": TelegramSettings(
            bot_token=SecretStr(VALID_BOT_TOKEN),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        ),
        "cryptobot": CryptoBotSettings(
            api_token=SecretStr("12345:cryptobot-test-token"),
            network="testnet",
        ),
        "bot": BotSettings(throttle_seconds=0.0),
        "delivery": DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
        "security": SecuritySettings(
            jwt_secret=SecretStr("a" * 48),
            admin_username="administrator",
            admin_password=SecretStr("super-secret-password"),
            cors_origins=("http://localhost:5173",),
        ),
    }
    groups.update(overrides)
    return Settings(**groups)
