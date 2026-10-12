"""Export the OpenAPI schema to a file.

The admin panel generates its TypeScript types from this file, so the frontend
cannot drift away from the API silently. Run it after changing any endpoint:

    make openapi
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.core.config import (
    AppSettings,
    BotSettings,
    CryptoBotSettings,
    DailySettings,
    DeliverySettings,
    Environment,
    LogFormat,
    LoyaltySettings,
    PostgresSettings,
    RedisSettings,
    ReferralSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from app.main import create_app

OUTPUT = Path(__file__).resolve().parents[1] / "openapi.json"

# The schema depends only on the route definitions, so placeholder settings are
# enough — no database, no bot token, no secrets from the environment.
_PLACEHOLDER = Settings(
    app=AppSettings(environment=Environment.LOCAL, log_format=LogFormat.CONSOLE),
    postgres=PostgresSettings(user="schema", password=SecretStr("schema"), db="schema"),
    redis=RedisSettings(),
    telegram=TelegramSettings(
        bot_token=SecretStr("123456789:AAHschema-only-token_for_openapi_export01"),
        bot_username="SchemaBot",
        use_webhook=False,
        webhook_secret=SecretStr("schema-only-webhook-secret"),
    ),
    cryptobot=CryptoBotSettings(api_token=SecretStr("1:schema")),
    bot=BotSettings(),
    delivery=DeliverySettings(),
    referral=ReferralSettings(),
    loyalty=LoyaltySettings(),
    daily=DailySettings(),
    security=SecuritySettings(
        jwt_secret=SecretStr("s" * 48),
        admin_username="schema",
        admin_password=SecretStr("schema-password"),
    ),
)


def build_schema() -> dict[str, Any]:
    """The OpenAPI schema of the current route definitions."""
    return create_app(_PLACEHOLDER).openapi()


def render(schema: dict[str, Any]) -> str:
    """The file's exact text: what ``main`` writes and the contract test compares."""
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """Write the current OpenAPI schema next to the backend package."""
    OUTPUT.write_text(render(build_schema()), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
