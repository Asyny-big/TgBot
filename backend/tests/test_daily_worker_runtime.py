"""The Daily worker process: its health route, its start-up checks, its wiring.

No database here: none of it may depend on one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from app import __version__
from app.bot.webhooks import HEALTH_PATH
from app.core.config import DailySettings
from app.core.container import Container
from app.core.resources import Resources
from app.daily_worker.health import Heartbeats, build_health_app
from app.daily_worker.loops import (
    EvaluatorLoop,
    ExpiryLoop,
    OutboxLoop,
    PlannerLoop,
    SenderLoop,
)
from app.daily_worker.runner import create_daily_worker, worker_configuration_problems
from app.domain.enums import DailyOutboxChannel
from app.infrastructure.telegram.daily_pacing import PacedDailySender, PacedTextSender
from app.infrastructure.telegram.daily_runtime import TelegramTextSender
from tests.daily_bot_harness import DAILY_BOT_TOKEN, DailyRecordingBot
from tests.settings_factory import VALID_BOT_TOKEN, build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

Client = TestClient[web.Request, web.Application]


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _client(heartbeats: Heartbeats) -> AsyncIterator[Client]:
    client: Client = TestClient(TestServer(build_health_app(heartbeats)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_the_worker_is_healthy_while_every_loop_turns() -> None:
    ticks = Ticks()
    heartbeats = Heartbeats(clock=ticks)
    heartbeats.expect("planner", within=180)
    heartbeats.expect("sender", within=420)
    ticks.now = 100

    async for client in _client(heartbeats):
        response = await client.get(HEALTH_PATH)
        body = await response.json()

    assert response.status == 200
    assert body == {
        "status": "alive",
        "version": __version__,
        "loops": {"planner": 100.0, "sender": 100.0},
        "stale": [],
    }


async def test_a_loop_that_stopped_turning_makes_the_worker_unhealthy() -> None:
    ticks = Ticks()
    heartbeats = Heartbeats(clock=ticks)
    heartbeats.expect("planner", within=180)
    heartbeats.expect("sender", within=420)
    ticks.now = 400
    heartbeats.beat("sender")

    async for client in _client(heartbeats):
        response = await client.get(HEALTH_PATH)
        body = await response.json()

    assert response.status == 503
    assert body["status"] == "stale"
    assert body["stale"] == ["planner"]


def test_the_worker_needs_a_daily_token_of_its_own() -> None:
    daily = DailySettings(bot_token=SecretStr(DAILY_BOT_TOKEN))

    assert worker_configuration_problems(build_settings(daily=DailySettings())) == (
        "DAILY_BOT_TOKEN is not set",
    )
    assert worker_configuration_problems(
        build_settings(daily=DailySettings(bot_token=SecretStr(VALID_BOT_TOKEN)))
    ) == ("DAILY_BOT_TOKEN must not be the shop bot's TELEGRAM_BOT_TOKEN",)
    # No webhook settings: the worker only sends.
    assert worker_configuration_problems(build_settings(daily=daily)) == ()


async def test_the_worker_assembles_five_loops_on_one_budget() -> None:
    settings = build_settings(daily=DailySettings(bot_token=SecretStr(DAILY_BOT_TOKEN)))
    resources = Resources.create(settings)
    container = Container.create(resources)
    daily_bot, shop_bot = DailyRecordingBot(), DailyRecordingBot()
    try:
        worker = create_daily_worker(container, daily_bot=daily_bot, shop_bot=shop_bot)
    finally:
        await daily_bot.session.close()
        await shop_bot.session.close()
        await container.crypto_payments.close()
        await resources.close()

    kinds = [type(loop) for loop in worker.loops]
    assert kinds == [PlannerLoop, SenderLoop, OutboxLoop, ExpiryLoop, EvaluatorLoop]
    assert sorted(worker.heartbeats.ages()) == sorted(loop.name for loop in worker.loops)

    sender = _attribute(worker.loops[1], "_delivery")
    outbox = _attribute(worker.loops[2], "_outbox")
    items = _attribute(sender, "sender")
    senders = _attribute(outbox, "senders")
    assert isinstance(items, PacedDailySender)
    assert isinstance(senders[DailyOutboxChannel.DAILY], PacedTextSender)
    # The shop bot only carries the revoked-token alert: no Daily budget for it.
    assert isinstance(senders[DailyOutboxChannel.SHOP], TelegramTextSender)
    # One pacer for everything the Daily bot sends.
    paced = {
        id(_attribute(items, "_pacer")),
        id(_attribute(senders[DailyOutboxChannel.DAILY], "_pacer")),
        id(_attribute(worker.loops[1], "_pacer")),
        id(_attribute(worker.loops[2], "_pacer")),
    }
    assert len(paced) == 1


def _attribute(owner: object, name: str) -> Any:
    return getattr(owner, name)
