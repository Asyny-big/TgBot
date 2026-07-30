"""Bot process entrypoint.

Two modes, one code path for everything below the transport:

* **webhook** (production): an aiohttp server receives Telegram updates and
  Crypto Pay notifications behind nginx;
* **long polling** (local development): no public URL needed.

Both modes run the reconciliation and housekeeping workers.

The process shuts down gracefully on ``SIGTERM``, which is what a container
runtime sends first: the workers are cancelled, the Telegram session and the
Crypto Pay client are closed and the pools are released. Without an explicit
handler Python would let the default disposition kill the process outright,
skipping every cleanup path.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.factory import create_checkout, create_dispatcher
from app.bot.webhooks import register_cryptobot_webhook, register_health_route
from app.bot.workers import HousekeepingWorker, ReconciliationWorker
from app.core.config import get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger
from app.core.resources import Resources
from app.infrastructure.telegram.factory import create_bot

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from app.core.config import Settings
    from app.services.checkout import CheckoutService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BotRuntime:
    """Everything one bot process needs, assembled once by ``main()``.

    The transports take this bundle instead of five separate arguments: they all
    travel together, they are all built in the same place, and a mode that needed
    only some of them would still be given the same process.
    """

    settings: Settings
    container: Container
    bot: Bot
    dispatcher: Dispatcher
    checkout: CheckoutService


def _start_workers(runtime: BotRuntime) -> list[asyncio.Task[None]]:
    """Launch the background loops that make lost notifications survivable."""
    reconciliation = ReconciliationWorker(
        purchases=runtime.container.purchases,
        checkout=runtime.checkout,
        crypto=runtime.container.crypto_payments,
        settings=runtime.settings.bot,
    )
    housekeeping = HousekeepingWorker(
        purchases=runtime.container.purchases,
        settings=runtime.settings.bot,
    )
    return [
        asyncio.create_task(reconciliation.run_forever(), name="reconciliation"),
        asyncio.create_task(housekeeping.run_forever(), name="housekeeping"),
    ]


async def _stop_workers(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Turn SIGTERM/SIGINT into a request to shut down cleanly."""
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        # Not every platform supports this; on those, the default disposition
        # applies and the process simply exits.
        with suppress(NotImplementedError):
            loop.add_signal_handler(received, stop.set)


async def _wait_for_shutdown(stop: asyncio.Event, transport: asyncio.Task[None]) -> None:
    """Wait until either a signal arrives or the transport stops on its own."""
    waiter = asyncio.create_task(stop.wait(), name="shutdown")
    try:
        await asyncio.wait({waiter, transport}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
    if transport.done():
        # Re-raise a transport failure instead of exiting as if asked to stop.
        transport.result()


async def run_polling(runtime: BotRuntime, stop: asyncio.Event) -> None:
    """Consume updates with long polling (local development)."""
    telegram = runtime.settings.telegram
    await runtime.bot.delete_webhook(drop_pending_updates=telegram.drop_pending_updates)
    tasks = _start_workers(runtime)
    logger.info("bot_started", mode="polling")
    polling = asyncio.create_task(
        runtime.dispatcher.start_polling(runtime.bot, handle_signals=False),
        name="polling",
    )
    try:
        await _wait_for_shutdown(stop, polling)
    finally:
        if not polling.done():
            await runtime.dispatcher.stop_polling()
            with suppress(asyncio.CancelledError):
                await polling
        await _stop_workers(tasks)


def _build_webhook_app(runtime: BotRuntime) -> web.Application:
    """Assemble the aiohttp application nginx forwards to."""
    settings = runtime.settings
    secret = settings.telegram.webhook_secret.get_secret_value()

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=runtime.dispatcher,
        bot=runtime.bot,
        secret_token=secret,
    ).register(app, path=settings.telegram.webhook_path)
    register_cryptobot_webhook(
        app,
        path=settings.cryptobot.webhook_path,
        client=runtime.container.crypto_payments,
        checkout=runtime.checkout,
    )
    register_health_route(app)
    setup_application(app, runtime.dispatcher, bot=runtime.bot)
    return app


async def run_webhook(runtime: BotRuntime, stop: asyncio.Event) -> None:
    """Serve Telegram and Crypto Pay webhooks behind nginx."""
    settings = runtime.settings
    await runtime.bot.set_webhook(
        url=settings.telegram.webhook_url,
        secret_token=settings.telegram.webhook_secret.get_secret_value(),
        drop_pending_updates=settings.telegram.drop_pending_updates,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )

    app = _build_webhook_app(runtime)
    tasks = _start_workers(runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.bot.webhook_host,
        port=settings.bot.webhook_port,
    )
    await site.start()
    logger.info(
        "bot_started",
        mode="webhook",
        webhook_url=settings.telegram.webhook_url,
        port=settings.bot.webhook_port,
    )
    try:
        await stop.wait()
    finally:
        await _stop_workers(tasks)
        # Releases the listening socket, so a restart never hits "address in use".
        await runner.cleanup()


async def main() -> None:
    """Build everything, run the selected mode, release everything."""
    settings = get_settings()
    configure_logging(level=settings.app.log_level, log_format=settings.app.log_format)

    resources = Resources.create(settings)
    container = Container.create(resources)
    bot = create_bot(settings)
    checkout = create_checkout(container, bot)
    runtime = BotRuntime(
        settings=settings,
        container=container,
        bot=bot,
        dispatcher=create_dispatcher(container, checkout),
        checkout=checkout,
    )

    checks = await resources.check()
    logger.info("bot_dependencies_checked", **checks)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        if settings.telegram.use_webhook:
            await run_webhook(runtime, stop)
        else:
            await run_polling(runtime, stop)
    finally:
        await bot.session.close()
        await container.crypto_payments.close()
        await resources.close()
        logger.info("bot_stopped")


def run() -> None:
    """Synchronous wrapper used by the container entrypoint."""
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
