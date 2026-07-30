"""Runtime behaviour of the bot process: liveness route and graceful shutdown.

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
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import __version__
from app.bot.runner import _install_signal_handlers, _wait_for_shutdown
from app.bot.webhooks import HEALTH_PATH, register_health_route

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
