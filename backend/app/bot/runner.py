"""Bot process entrypoint.

Two modes, one code path for everything below the transport:

* **webhook** (production): an aiohttp server receives Telegram updates and
  Crypto Pay notifications behind nginx;
* **long polling** (local development): no public URL needed.

Both modes run the reconciliation and housekeeping workers.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.factory import create_checkout, create_dispatcher
from app.bot.webhooks import register_cryptobot_webhook
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


def _start_workers(
    container: Container,
    checkout: CheckoutService,
) -> list[asyncio.Task[None]]:
    """Launch the background loops that make lost notifications survivable."""
    reconciliation = ReconciliationWorker(
        purchases=container.purchases,
        checkout=checkout,
        crypto=container.crypto_payments,
        settings=container.settings.bot,
    )
    housekeeping = HousekeepingWorker(
        purchases=container.purchases,
        settings=container.settings.bot,
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


async def run_polling(
    settings: Settings,
    container: Container,
    bot: Bot,
    dispatcher: Dispatcher,
    checkout: CheckoutService,
) -> None:
    """Consume updates with long polling (local development)."""
    await bot.delete_webhook(drop_pending_updates=settings.telegram.drop_pending_updates)
    tasks = _start_workers(container, checkout)
    logger.info("bot_started", mode="polling")
    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        await _stop_workers(tasks)


async def run_webhook(
    settings: Settings,
    container: Container,
    bot: Bot,
    dispatcher: Dispatcher,
    checkout: CheckoutService,
) -> None:
    """Serve Telegram and Crypto Pay webhooks behind nginx."""
    secret = settings.telegram.webhook_secret.get_secret_value()
    await bot.set_webhook(
        url=settings.telegram.webhook_url,
        secret_token=secret,
        drop_pending_updates=settings.telegram.drop_pending_updates,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )

    app = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(
        app,
        path=settings.telegram.webhook_path,
    )
    register_cryptobot_webhook(
        app,
        path=settings.cryptobot.webhook_path,
        client=container.crypto_payments,
        checkout=checkout,
    )
    setup_application(app, dispatcher, bot=bot)

    tasks = _start_workers(container, checkout)
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
        await asyncio.Event().wait()
    finally:
        await _stop_workers(tasks)
        await runner.cleanup()


async def main() -> None:
    """Build everything, run the selected mode, release everything."""
    settings = get_settings()
    configure_logging(level=settings.app.log_level, log_format=settings.app.log_format)

    resources = Resources.create(settings)
    container = Container.create(resources)
    bot = create_bot(settings)
    checkout = create_checkout(container, bot)
    dispatcher = create_dispatcher(container, checkout)

    checks = await resources.check()
    logger.info("bot_dependencies_checked", **checks)

    try:
        if settings.telegram.use_webhook:
            await run_webhook(settings, container, bot, dispatcher, checkout)
        else:
            await run_polling(settings, container, bot, dispatcher, checkout)
    finally:
        await bot.session.close()
        await container.crypto_payments.close()
        await resources.close()
        logger.info("bot_stopped")


def run() -> None:
    """Synchronous wrapper used by the container entrypoint."""
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
