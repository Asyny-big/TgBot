"""Configuration validation and derived value tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    CRYPTOBOT_MAINNET_API,
    CRYPTOBOT_TESTNET_API,
    AppSettings,
    CryptoBotSettings,
    Environment,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    TelegramSettings,
    get_settings,
)
from tests.conftest import VALID_BOT_TOKEN, build_settings


def test_postgres_dsn_escapes_credentials() -> None:
    settings = PostgresSettings(
        host="db",
        port=5433,
        user="shop@user",
        password=SecretStr("p@ss/word"),
        db="shop",
    )
    assert settings.dsn == "postgresql+asyncpg://shop%40user:p%40ss%2Fword@db:5433/shop"


def test_redis_dsn_with_and_without_password() -> None:
    plain = RedisSettings(host="cache", port=6380, db=3)
    assert plain.dsn == "redis://cache:6380/3"

    secured = RedisSettings(host="cache", password=SecretStr("p@ss"))
    assert secured.dsn == "redis://:p%40ss@cache:6379/0"


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled"):
        AppSettings(environment=Environment.PRODUCTION, debug=True)


def test_api_prefix_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        AppSettings(api_prefix="api/v1")

    assert AppSettings(api_prefix="/api/v2/").api_prefix == "/api/v2"


@pytest.mark.parametrize("username", ["@MyShopBot", "MyShopBot", " MyShopBot "])
def test_bot_username_is_normalised(username: str) -> None:
    settings = TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username=username,
        use_webhook=False,
        webhook_secret=SecretStr("webhook-secret-value"),
    )
    assert settings.bot_username == "MyShopBot"


@pytest.mark.parametrize("username", ["bot", "1invalid", "way_too_long_username_" * 3])
def test_invalid_bot_username_is_rejected(username: str) -> None:
    with pytest.raises(ValidationError, match="bot_username"):
        TelegramSettings(
            bot_token=SecretStr(VALID_BOT_TOKEN),
            bot_username=username,
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        )


def test_invalid_bot_token_is_rejected() -> None:
    with pytest.raises(ValidationError, match="bot_token"):
        TelegramSettings(
            bot_token=SecretStr("not-a-token"),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        )


def test_webhook_mode_requires_base_url() -> None:
    with pytest.raises(ValidationError, match="webhook_base_url is required"):
        TelegramSettings(
            bot_token=SecretStr(VALID_BOT_TOKEN),
            bot_username="MyShopBot",
            use_webhook=True,
            webhook_secret=SecretStr("webhook-secret-value"),
        )


def test_webhook_url_and_deep_link_are_built() -> None:
    settings = TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username="MyShopBot",
        use_webhook=True,
        webhook_base_url="https://shop.example.com/",
        webhook_path="/webhook/telegram/",
        webhook_secret=SecretStr("webhook-secret-value"),
    )
    assert settings.webhook_url == "https://shop.example.com/webhook/telegram"
    assert settings.deep_link("vip1") == "https://t.me/MyShopBot?start=vip1"


def test_webhook_url_unavailable_in_polling_mode() -> None:
    settings = TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username="MyShopBot",
        use_webhook=False,
        webhook_secret=SecretStr("webhook-secret-value"),
    )
    with pytest.raises(RuntimeError, match="webhook mode"):
        _ = settings.webhook_url


def test_short_webhook_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="webhook_secret"):
        TelegramSettings(
            bot_token=SecretStr(VALID_BOT_TOKEN),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("short"),
        )


def test_cryptobot_api_base_url_follows_network() -> None:
    mainnet = CryptoBotSettings(api_token=SecretStr("1:token"), network="mainnet")
    testnet = CryptoBotSettings(api_token=SecretStr("1:token"), network="testnet")
    assert mainnet.api_base_url == CRYPTOBOT_MAINNET_API
    assert testnet.api_base_url == CRYPTOBOT_TESTNET_API


def test_weak_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="jwt_secret"):
        SecuritySettings(
            jwt_secret=SecretStr("too-short"),
            admin_username="administrator",
            admin_password=SecretStr("super-secret-password"),
        )


def test_weak_admin_password_is_rejected() -> None:
    with pytest.raises(ValidationError, match="admin_password"):
        SecuritySettings(
            jwt_secret=SecretStr("a" * 48),
            admin_username="administrator",
            admin_password=SecretStr("short"),
        )


def test_cors_origins_accept_comma_separated_string() -> None:
    settings = SecuritySettings(
        jwt_secret=SecretStr("a" * 48),
        admin_username="administrator",
        admin_password=SecretStr("super-secret-password"),
        cors_origins="https://a.example.com, https://b.example.com ",  # type: ignore[arg-type]
    )
    assert settings.cors_origins == ("https://a.example.com", "https://b.example.com")


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = build_settings()
    dumped = repr(settings)
    assert "shop-password" not in dumped
    assert "super-secret-password" not in dumped
    assert VALID_BOT_TOKEN not in dumped


def test_settings_are_frozen() -> None:
    settings: Any = build_settings()
    with pytest.raises(ValidationError):
        settings.app = AppSettings()


def test_get_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "APP_ENVIRONMENT": "production",
        "APP_LOG_FORMAT": "json",
        "APP_DOCS_ENABLED": "false",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_USER": "shop",
        "POSTGRES_PASSWORD": "shop-password",
        "POSTGRES_DB": "shop",
        "REDIS_HOST": "redis",
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "TELEGRAM_BOT_USERNAME": "@MyShopBot",
        "TELEGRAM_USE_WEBHOOK": "true",
        "TELEGRAM_WEBHOOK_BASE_URL": "https://shop.example.com",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret-value",
        "CRYPTOBOT_API_TOKEN": "12345:cryptobot-token",
        "SECURITY_JWT_SECRET": "b" * 48,
        "SECURITY_ADMIN_USERNAME": "administrator",
        "SECURITY_ADMIN_PASSWORD": "super-secret-password",
        "SECURITY_CORS_ORIGINS": "https://admin.example.com",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.app.environment is Environment.PRODUCTION
        assert settings.telegram.bot_username == "MyShopBot"
        assert settings.telegram.webhook_url == "https://shop.example.com/webhook/telegram"
        assert settings.security.cors_origins == ("https://admin.example.com",)
        assert settings.postgres.dsn.startswith("postgresql+asyncpg://shop:")
    finally:
        get_settings.cache_clear()


def test_get_settings_fails_without_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_blank_redis_password_is_treated_as_absent() -> None:
    settings = RedisSettings(host="cache", password="")  # type: ignore[arg-type]
    assert settings.password is None
    assert settings.dsn == "redis://cache:6379/0"
