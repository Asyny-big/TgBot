"""Runtime behaviour of the bot process: liveness route, graceful shutdown, menu.

These are the pieces a container runtime interacts with. They are tested here
without a database, because neither of them may depend on one: a liveness probe
that fails during a PostgreSQL outage would turn a dependency blip into a restart
loop, and a shutdown path that needed the database could not run while it is down.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

import pytest
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeAllPrivateChats
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import __version__
from app.bot.handlers.bonuses import BONUS_COMMAND
from app.bot.handlers.progress import PROGRESS_COMMAND
from app.bot.runner import _install_signal_handlers, _publish_commands, _wait_for_shutdown
from app.bot.texts import BONUS_SECTION_BUTTON, PROGRESS_SECTION_BUTTON
from app.bot.webhooks import HEALTH_PATH, register_health_route
from app.core.config import LoyaltySettings, ReferralSettings
from tests.bot_harness import RecordingBot
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def health_client() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    """A client wired straight into an application with only the health route."""
    app = web.Application()
    register_health_route(app)
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_the_liveness_route_reports_the_running_version(
    health_client: TestClient[web.Request, web.Application],
) -> None:
    """The container healthcheck must get a cheap, unambiguous answer."""
    response = await health_client.get(HEALTH_PATH)

    assert response.status == 200
    assert await response.json() == {"status": "alive", "version": __version__}


async def test_the_liveness_route_is_read_only(
    health_client: TestClient[web.Request, web.Application],
) -> None:
    """Nothing may be triggered by probing liveness."""
    response = await health_client.post(HEALTH_PATH)

    assert response.status == 405


async def test_the_liveness_path_is_not_a_webhook_path() -> None:
    """It must never collide with a provider route nginx forwards."""
    assert not HEALTH_PATH.startswith("/webhook/")


async def test_sigterm_asks_the_process_to_stop() -> None:
    """A container stop must reach the shutdown path, not kill the process."""
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)

    assert stop.is_set()


async def test_shutdown_returns_while_the_transport_is_still_serving() -> None:
    """The waiter reacts to the signal; stopping the transport is the caller's job."""
    stop = asyncio.Event()
    transport = asyncio.create_task(asyncio.sleep(30))

    stop.set()
    await asyncio.wait_for(_wait_for_shutdown(stop, transport), timeout=2)

    assert not transport.done()
    transport.cancel()


async def test_a_transport_failure_is_re_raised_instead_of_looking_like_a_stop() -> None:
    """A webhook server that dies must not be mistaken for a clean shutdown."""

    async def explode() -> None:
        msg = "port already in use"
        raise OSError(msg)

    stop = asyncio.Event()
    transport = asyncio.create_task(explode())

    with pytest.raises(OSError, match="port already in use"):
        await asyncio.wait_for(_wait_for_shutdown(stop, transport), timeout=2)


async def test_a_transport_that_finishes_on_its_own_ends_the_wait() -> None:
    """Long polling that stops by itself must not hang the process."""

    async def finish() -> None:
        return

    stop = asyncio.Event()
    transport = asyncio.create_task(finish())

    await asyncio.wait_for(_wait_for_shutdown(stop, transport), timeout=2)

    assert transport.done()
    assert not stop.is_set()


# --------------------------------------------------------------------------- #
# The Menu button                                                             #
# --------------------------------------------------------------------------- #

BONUS = (BONUS_COMMAND, BONUS_SECTION_BUTTON)
PROGRESS = (PROGRESS_COMMAND, PROGRESS_SECTION_BUTTON)


@pytest.mark.parametrize(
    ("referral", "loyalty", "menu"),
    [
        (True, True, [BONUS, PROGRESS]),
        (True, False, [BONUS]),
        (False, True, [PROGRESS]),
    ],
    ids=["both", "referral-only", "loyalty-only"],
)
async def test_the_menu_lists_every_section_that_is_switched_on(
    referral: bool,
    loyalty: bool,
    menu: list[tuple[str, str]],
) -> None:
    bot = RecordingBot()
    settings = build_settings(
        referral=ReferralSettings(enabled=referral),
        loyalty=LoyaltySettings(enabled=loyalty),
    )

    await _publish_commands(bot, settings)
    await bot.session.close()

    (published,) = bot.methods(SetMyCommands)
    assert [(command.command, command.description) for command in published.commands] == menu
    assert isinstance(published.scope, BotCommandScopeAllPrivateChats)


async def test_the_menu_is_left_alone_when_no_section_is_on() -> None:
    bot = RecordingBot()
    settings = build_settings(
        referral=ReferralSettings(enabled=False),
        loyalty=LoyaltySettings(enabled=False),
    )

    await _publish_commands(bot, settings)
    await bot.session.close()

    assert bot.methods(SetMyCommands) == []
