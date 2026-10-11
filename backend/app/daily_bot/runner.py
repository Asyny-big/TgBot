"""Daily Content bot process entrypoint.

Two modes, one code path below the transport:

* **webhook** (production): an aiohttp server behind nginx receives the Daily
  bot's updates on ``DAILY_WEBHOOK_PATH``, authenticated by Telegram's secret
  token header, and serves ``/internal/health`` for the container healthcheck;
* **long polling** (local development).

The process refuses to start without its own configuration (token, username,
and in webhook mode the URL and secret) — loudly, at once, and without
affecting any other process. It shuts down cleanly on ``SIGTERM`` exactly like
the shop bot.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.runner import _install_signal_handlers, _wait_for_shutdown
from app.bot.webhooks import register_health_route
from app.core.config import get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger
from app.core.resources import Resources
from app.daily_bot.factory import (
    create_daily_dispatcher,
    create_daily_services,
    create_draft_storage,
)
from app.daily_bot.texts import COMMANDS, OWNER_COMMANDS
from app.infrastructure.telegram.factory import create_daily_bot

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from app.core.config import DailySettings, Settings

logger = get_logger(__name__)

ALLOWED_UPDATES: Final = ("message", "callback_query", "my_chat_member")
"""``my_chat_member`` is how the bot learns that a buyer blocked or unblocked it."""


@dataclass(frozen=True, slots=True)
class DailyRuntime:
    """Everything one Daily bot process needs, assembled once by ``main()``."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher


def configuration_problems(settings: Settings) -> tuple[str, ...]:
    """What keeps the Daily bot process from starting; empty when nothing does.

    Besides its own settings, the Daily bot must never share the shop bot's
    identity: one token in two processes would let the Daily bot's
    ``setWebhook`` take the shop bot's updates away.
    """
    daily, shop = settings.daily, settings.telegram
    problems = list(daily.bot_problems())
    if (
        daily.bot_token is not None
        and daily.bot_token.get_secret_value() == shop.bot_token.get_secret_value()
    ):
        problems.append("DAILY_BOT_TOKEN must not be the shop bot's TELEGRAM_BOT_TOKEN")
    if daily.bot_username and daily.bot_username.lower() == shop.bot_username.lower():
        problems.append("DAILY_BOT_USERNAME must not be the shop bot's username")
    if daily.use_webhook and daily.webhook_path == shop.webhook_path:
        problems.append("DAILY_WEBHOOK_PATH must differ from TELEGRAM_WEBHOOK_PATH")
    return tuple(problems)


def build_webhook_app(settings: DailySettings, dispatcher: Dispatcher, bot: Bot) -> web.Application:
    """The aiohttp application nginx forwards the Daily bot's webhook to.

    Raises:
        RuntimeError: no webhook secret is configured.
    """
    if settings.webhook_secret is None:
        message = "DAILY_WEBHOOK_SECRET is required in webhook mode"
        raise RuntimeError(message)
    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret.get_secret_value(),
    ).register(app, path=settings.webhook_path)
    register_health_route(app)
    setup_application(app, dispatcher, bot=bot)
    return app


async def run_polling(runtime: DailyRuntime, stop: asyncio.Event) -> None:
    """Consume updates with long polling (local development)."""
    daily = runtime.settings.daily
    await runtime.bot.delete_webhook(drop_pending_updates=daily.drop_pending_updates)
    logger.info("daily_bot_started", mode="polling")
    polling = asyncio.create_task(
        runtime.dispatcher.start_polling(
            runtime.bot,
            handle_signals=False,
            allowed_updates=list(ALLOWED_UPDATES),
        ),
        name="daily-polling",
    )
    try:
        await _wait_for_shutdown(stop, polling)
    finally:
        if not polling.done():
            await runtime.dispatcher.stop_polling()
            with suppress(asyncio.CancelledError):
                await polling


async def run_webhook(runtime: DailyRuntime, stop: asyncio.Event) -> None:
    """Serve the Daily bot's webhook behind nginx."""
    daily = runtime.settings.daily
    if daily.webhook_secret is None:  # pragma: no cover — bot_problems() refuses first
        message = "DAILY_WEBHOOK_SECRET is required in webhook mode"
        raise RuntimeError(message)
    await runtime.bot.set_webhook(
        url=daily.webhook_url,
        secret_token=daily.webhook_secret.get_secret_value(),
        drop_pending_updates=daily.drop_pending_updates,
        allowed_updates=list(ALLOWED_UPDATES),
    )
    runner = web.AppRunner(build_webhook_app(daily, runtime.dispatcher, runtime.bot))
    await runner.setup()
    site = web.TCPSite(runner, host=daily.bot_host, port=daily.bot_port)
    await site.start()
    logger.info("daily_bot_started", mode="webhook", port=daily.bot_port)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


async def _publish_commands(bot: Bot, owner_ids: tuple[int, ...]) -> None:
    """The command menus: the buyers', and the owner's in the owner's own chats.

    Cosmetic: a failure is logged, not raised.
    """
    try:
        await bot.set_my_commands(
            [BotCommand(command=command, description=text) for command, text in COMMANDS],
            scope=BotCommandScopeAllPrivateChats(),
        )
        for owner_id in owner_ids:
            await bot.set_my_commands(
                [BotCommand(command=command, description=text) for command, text in OWNER_COMMANDS],
                scope=BotCommandScopeChat(chat_id=owner_id),
            )
    except Exception as error:
        logger.warning("daily_bot_commands_not_published", error=str(error))


async def main() -> None:
    """Check the configuration, build everything, run the selected mode."""
    settings = get_settings()
    configure_logging(level=settings.app.log_level, log_format=settings.app.log_format)
    problems = configuration_problems(settings)
    if problems:
        logger.error("daily_bot_not_configured", problems=list(problems))
        message = "The Daily bot is not configured: " + "; ".join(problems)
        raise SystemExit(message)

    resources = Resources.create(settings)
    container = Container.create(resources)
    bot = create_daily_bot(settings.daily)
    services = create_daily_services(container, bot)
    runtime = DailyRuntime(
        settings=settings,
        bot=bot,
        dispatcher=create_daily_dispatcher(
            services,
            locks=container.locks,
            throttle_seconds=settings.bot.throttle_seconds,
            storage=create_draft_storage(resources.cache.client),
        ),
    )

    checks = await resources.check()
    logger.info("daily_bot_dependencies_checked", enabled=settings.daily.enabled, **checks)
    await _publish_commands(bot, settings.daily.admin_telegram_ids)

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        if settings.daily.use_webhook:
            await run_webhook(runtime, stop)
        else:
            await run_polling(runtime, stop)
    finally:
        await services.albums.drain()
        await services.draft_albums.drain()
        await bot.session.close()
        await container.crypto_payments.close()
        await resources.close()
        logger.info("daily_bot_stopped")


def run() -> None:
    """Synchronous wrapper used by the container entrypoint."""
    with suppress(KeyboardInterrupt):
        asyncio.run(main())


__all__ = ["ALLOWED_UPDATES", "build_webhook_app", "configuration_problems", "main", "run"]
