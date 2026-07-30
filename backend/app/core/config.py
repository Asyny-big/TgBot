"""Typed application configuration.

Every value comes from the environment (or the repository level ``.env`` file in
local development). Configuration is validated eagerly on process start-up: a
missing or malformed value must crash the process instead of surfacing as a
runtime failure in the middle of a payment flow.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR: Final = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Final = BACKEND_DIR.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

_BOT_USERNAME_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_BOT_TOKEN_RE: Final = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

MIN_WEBHOOK_SECRET_LENGTH: Final = 16
MIN_JWT_SECRET_LENGTH: Final = 32
MIN_ADMIN_PASSWORD_LENGTH: Final = 12

CRYPTOBOT_MAINNET_API: Final = "https://pay.crypt.bot/api"
CRYPTOBOT_TESTNET_API: Final = "https://testnet-pay.crypt.bot/api"


class Environment(StrEnum):
    """Deployment environment of the running process."""

    LOCAL = "local"
    TESTING = "testing"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self is Environment.PRODUCTION


class LogFormat(StrEnum):
    """Rendering style for structlog output."""

    JSON = "json"
    CONSOLE = "console"


def _settings_config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        secrets_dir=None,
    )


class AppSettings(BaseSettings):
    """Process-wide behaviour: naming, logging and HTTP surface."""

    model_config = _settings_config("APP_")

    name: str = "Telegram Digital Shop"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat = LogFormat.JSON
    api_prefix: str = "/api/v1"
    docs_enabled: bool = True
    host: str = "0.0.0.0"  # noqa: S104 — the container is only reachable through nginx
    port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("api_prefix")
    @classmethod
    def _validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = "api_prefix must start with '/'"
            raise ValueError(msg)
        return value.rstrip("/")

    @model_validator(mode="after")
    def _harden_production(self) -> Self:
        if self.environment.is_production and self.debug:
            msg = "debug must be disabled in the production environment"
            raise ValueError(msg)
        return self


class PostgresSettings(BaseSettings):
    """PostgreSQL connection and pool configuration."""

    model_config = _settings_config("POSTGRES_")

    host: str = "postgres"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str
    password: SecretStr
    db: str
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=100)
    pool_timeout: float = Field(default=30.0, gt=0)
    pool_recycle: int = Field(default=1800, gt=0)
    echo: bool = False

    @property
    def dsn(self) -> str:
        """SQLAlchemy async DSN (asyncpg driver)."""
        user = quote(self.user, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        database = quote(self.db, safe="")
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{database}"


class RedisSettings(BaseSettings):
    """Redis connection configuration."""

    model_config = _settings_config("REDIS_")

    host: str = "redis"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    password: SecretStr | None = None
    max_connections: int = Field(default=20, ge=1, le=500)
    socket_timeout: float = Field(default=5.0, gt=0)
    # Distributed locks always expire: a crashed process must not block a buyer.
    lock_ttl_seconds: float = Field(default=45.0, ge=5.0, le=300.0)
    lock_wait_seconds: float = Field(default=0.0, ge=0.0, le=30.0)

    @field_validator("password", mode="before")
    @classmethod
    def _blank_password_means_none(cls, value: object) -> object:
        """Treat ``REDIS_PASSWORD=`` in an env file as "no password"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def dsn(self) -> str:
        """Redis DSN including credentials when configured."""
        credentials = ""
        if self.password is not None:
            credentials = f":{quote(self.password.get_secret_value(), safe='')}@"
        return f"redis://{credentials}{self.host}:{self.port}/{self.db}"


class TelegramSettings(BaseSettings):
    """Bot credentials and update delivery mode."""

    model_config = _settings_config("TELEGRAM_")

    bot_token: SecretStr
    bot_username: str
    use_webhook: bool = True
    webhook_base_url: str | None = None
    webhook_path: str = "/webhook/telegram"
    webhook_secret: SecretStr
    drop_pending_updates: bool = True

    @field_validator("bot_username", mode="before")
    @classmethod
    def _normalise_username(cls, value: str) -> str:
        candidate = value.strip().removeprefix("@")
        if not _BOT_USERNAME_RE.match(candidate):
            msg = "bot_username must be a valid Telegram username without '@'"
            raise ValueError(msg)
        return candidate

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: SecretStr) -> SecretStr:
        if not _BOT_TOKEN_RE.match(value.get_secret_value()):
            msg = "bot_token does not look like a Telegram bot token (<id>:<secret>)"
            raise ValueError(msg)
        return value

    @field_validator("webhook_path")
    @classmethod
    def _validate_webhook_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = "webhook_path must start with '/'"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("webhook_secret")
    @classmethod
    def _validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < MIN_WEBHOOK_SECRET_LENGTH:
            msg = f"webhook_secret must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters long"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _require_base_url_for_webhook(self) -> Self:
        if self.use_webhook and not self.webhook_base_url:
            msg = "webhook_base_url is required when use_webhook is enabled"
            raise ValueError(msg)
        return self

    @property
    def webhook_url(self) -> str:
        """Absolute URL registered in Telegram via ``setWebhook``."""
        if self.webhook_base_url is None:
            msg = "webhook_url is only available when webhook mode is configured"
            raise RuntimeError(msg)
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"

    def deep_link(self, slug: str) -> str:
        """Build the public deep link that opens the given product card."""
        return f"https://t.me/{self.bot_username}?start={slug}"


class CryptoBotSettings(BaseSettings):
    """CryptoBot (Crypto Pay API) credentials and invoice defaults."""

    model_config = _settings_config("CRYPTOBOT_")

    api_token: SecretStr
    network: Literal["mainnet", "testnet"] = "mainnet"
    asset: str = "USDT"
    invoice_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    request_timeout: float = Field(default=15.0, gt=0)

    @property
    def api_base_url(self) -> str:
        """Crypto Pay API endpoint matching the configured network."""
        if self.network == "testnet":
            return CRYPTOBOT_TESTNET_API
        return CRYPTOBOT_MAINNET_API


class DeliverySettings(BaseSettings):
    """Retry policy for handing the purchased link to the buyer."""

    model_config = _settings_config("DELIVERY_")

    max_attempts: int = Field(default=4, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=1.0, gt=0, le=60.0)
    max_backoff_seconds: float = Field(default=20.0, gt=0, le=300.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    # Random jitter spreads retries so a Telegram outage does not produce a
    # synchronised thundering herd when it ends.
    jitter_ratio: float = Field(default=0.25, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> Self:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            msg = "max_backoff_seconds must not be smaller than initial_backoff_seconds"
            raise ValueError(msg)
        return self


class SecuritySettings(BaseSettings):
    """Admin authentication and browser-facing security policy."""

    model_config = _settings_config("SECURITY_")

    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)
    admin_username: str = Field(min_length=3, max_length=64)
    admin_password: SecretStr
    # NoDecode keeps pydantic-settings from JSON-decoding the raw env value, so
    # the comma separated form below is parsed by the validator instead.
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_JWT_SECRET_LENGTH:
            msg = f"jwt_secret must be at least {MIN_JWT_SECRET_LENGTH} characters long"
            raise ValueError(msg)
        return value

    @field_validator("admin_password")
    @classmethod
    def _validate_admin_password(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_ADMIN_PASSWORD_LENGTH:
            msg = f"admin_password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters long"
            raise ValueError(msg)
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value


class Settings(BaseModel):
    """Aggregate of every configuration group used by the application."""

    model_config = ConfigDict(frozen=True)

    app: AppSettings
    postgres: PostgresSettings
    redis: RedisSettings
    telegram: TelegramSettings
    cryptobot: CryptoBotSettings
    delivery: DeliverySettings
    security: SecuritySettings


def _load[SettingsT: BaseSettings](settings_class: type[SettingsT]) -> SettingsT:
    """Instantiate a settings group; every field value comes from the environment."""
    return settings_class()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings(
        app=_load(AppSettings),
        postgres=_load(PostgresSettings),
        redis=_load(RedisSettings),
        telegram=_load(TelegramSettings),
        cryptobot=_load(CryptoBotSettings),
        delivery=_load(DeliverySettings),
        security=_load(SecuritySettings),
    )
