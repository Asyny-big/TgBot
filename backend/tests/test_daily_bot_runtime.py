"""The Daily bot process: its webhook, its liveness route, its start-up checks.

No database here: the transport and the configuration checks must not need one.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr, ValidationError

from app import __version__
from app.bot.webhooks import HEALTH_PATH
from app.core.config import DailySettings
from app.daily_bot.runner import ALLOWED_UPDATES, build_webhook_app, configuration_problems
from tests.daily_bot_harness import DAILY_BOT_TOKEN, DailyRecordingBot, buyer, message_update
from tests.settings_factory import VALID_BOT_TOKEN, build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiogram.types import Message

SECRET = "daily-webhook-secret-0123456789"  # noqa: S105
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 — a header name

Client = TestClient[web.Request, web.Application]


def _daily(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {
        "bot_token": SecretStr(DAILY_BOT_TOKEN),
        "bot_username": "VipDailyBot",
        "use_webhook": True,
        "webhook_base_url": "https://shop.example.com",
        "webhook_secret": SecretStr(SECRET),
    }
    values.update(overrides)
    return DailySettings(**values)


class Webhook:
    """A client on the Daily bot's aiohttp application and what reached the dispatcher."""

    def __init__(self, client: Client, received: asyncio.Queue[str]) -> None:
        self.client = client
        self.received = received

    async def post(self, payload: dict[str, Any], *, secret: str | None) -> int:
        headers = {} if secret is None else {SECRET_HEADER: secret}
        response = await self.client.post("/webhook/daily", json=payload, headers=headers)
        return response.status


@pytest.fixture
async def webhook() -> AsyncIterator[Webhook]:
    received: asyncio.Queue[str] = asyncio.Queue()
    dispatcher = Dispatcher()

    @dispatcher.message()
    async def _record(message: Message) -> None:
        await received.put(message.text or "")

    app = build_webhook_app(_daily(), dispatcher, DailyRecordingBot())
    client: Client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield Webhook(client, received)
    finally:
        await client.close()


def _update_payload(text: str) -> dict[str, Any]:
    update = message_update(text, user=buyer(4242))
    return update.model_dump(mode="json", exclude_none=True, by_alias=True)


# --------------------------------------------------------------------------- #
# Webhook and liveness                                                        #
# --------------------------------------------------------------------------- #


async def test_the_liveness_route_answers_without_a_secret(webhook: Webhook) -> None:
    response = await webhook.client.get(HEALTH_PATH)

    assert response.status == 200
    assert await response.json() == {"status": "alive", "version": __version__}


async def test_an_update_with_the_secret_reaches_the_dispatcher(webhook: Webhook) -> None:
    assert await webhook.post(_update_payload("/start"), secret=SECRET) == 200

    assert await asyncio.wait_for(webhook.received.get(), timeout=5) == "/start"


@pytest.mark.parametrize("secret", [None, "wrong-secret-wrong-secret", SECRET.upper()])
async def test_an_update_without_the_right_secret_is_refused(
    webhook: Webhook, secret: str | None
) -> None:
    assert await webhook.post(_update_payload("/start"), secret=secret) == 401

    await asyncio.sleep(0.05)
    assert webhook.received.empty()


async def test_the_shop_bots_webhook_path_is_not_served(webhook: Webhook) -> None:
    response = await webhook.client.post(
        "/webhook/telegram",
        json=_update_payload("/start"),
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status == 404
    assert webhook.received.empty()


def test_the_webhook_app_refuses_to_exist_without_a_secret() -> None:
    with pytest.raises(RuntimeError, match="DAILY_WEBHOOK_SECRET"):
        build_webhook_app(_daily(webhook_secret=None), Dispatcher(), DailyRecordingBot())


def test_the_bot_hears_about_being_blocked() -> None:
    assert "my_chat_member" in ALLOWED_UPDATES
    assert "message" in ALLOWED_UPDATES


# --------------------------------------------------------------------------- #
# Start-up checks                                                             #
# --------------------------------------------------------------------------- #


def test_a_complete_configuration_has_no_problems() -> None:
    assert configuration_problems(build_settings(daily=_daily())) == ()


def test_nothing_configured_names_every_missing_setting() -> None:
    problems = configuration_problems(build_settings(daily=DailySettings()))

    assert problems == (
        "DAILY_BOT_TOKEN is not set",
        "DAILY_BOT_USERNAME is not set",
        "DAILY_WEBHOOK_BASE_URL is required in webhook mode",
        "DAILY_WEBHOOK_SECRET is required in webhook mode",
    )


def test_polling_needs_no_webhook_settings() -> None:
    daily = _daily(use_webhook=False, webhook_base_url=None, webhook_secret=None)

    assert configuration_problems(build_settings(daily=daily)) == ()


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        (
            {"bot_token": SecretStr(VALID_BOT_TOKEN)},
            "DAILY_BOT_TOKEN must not be the shop bot's TELEGRAM_BOT_TOKEN",
        ),
        (
            {"bot_username": "@myshopbot"},
            "DAILY_BOT_USERNAME must not be the shop bot's username",
        ),
        (
            {"webhook_path": "/webhook/telegram"},
            "DAILY_WEBHOOK_PATH must differ from TELEGRAM_WEBHOOK_PATH",
        ),
    ],
)
def test_the_daily_bot_never_shares_the_shop_bots_identity(
    overrides: dict[str, Any], problem: str
) -> None:
    assert configuration_problems(build_settings(daily=_daily(**overrides))) == (problem,)


# --------------------------------------------------------------------------- #
# DAILY_* bot settings                                                        #
# --------------------------------------------------------------------------- #


def test_the_bot_settings_are_optional_for_every_other_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("DAILY_BOT_TOKEN", "DAILY_BOT_USERNAME", "DAILY_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = DailySettings()

    assert settings.bot_token is None
    assert settings.bot_username is None
    assert settings.webhook_path == "/webhook/daily"
    assert settings.bot_port == 8082


def test_the_bot_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_BOT_TOKEN", DAILY_BOT_TOKEN)
    monkeypatch.setenv("DAILY_BOT_USERNAME", "@VipDailyBot")
    monkeypatch.setenv("DAILY_WEBHOOK_BASE_URL", "https://shop.example.com/")
    monkeypatch.setenv("DAILY_WEBHOOK_SECRET", SECRET)
    settings = DailySettings()

    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == DAILY_BOT_TOKEN
    assert settings.bot_username == "VipDailyBot"
    assert settings.webhook_url == "https://shop.example.com/webhook/daily"
    assert settings.bot_problems() == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_token": "not-a-token"},
        {"bot_username": "has space"},
        {"webhook_path": "webhook/daily"},
        {"webhook_secret": "too-short"},
        {"bot_port": 0},
    ],
)
def test_malformed_bot_settings_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DailySettings(**overrides)


def test_the_webhook_url_needs_a_base_url() -> None:
    with pytest.raises(RuntimeError):
        _ = _daily(webhook_base_url=None).webhook_url
