"""Daily worker process entrypoint: ``python -m app.daily_worker``.

The Daily Content runtime, apart from the bot that talks to buyers:

* **planner** — opens each buyer's delivery once their window opens;
* **sender** — sends the deliveries due, retries, takes over dead attempts;
* **outbox** — sends the owner's alerts and the buyers' notices;
* **expiry** — closes what passed its deadline, giving items back;
* **evaluator** — the stock, the failures, the token: the owner's alerts.

Everything it sends goes through one budget — at most
``DAILY_SEND_RATE_PER_SECOND``, the whole worker paused after Telegram's 429.

The process serves one route, ``/internal/health``, for the container's
health check. It starts — and idles — while Daily Content is off, so it can
be deployed before the launch. It refuses to start without a Daily bot token
of its own. A loop that ever ends on its own is a bug: the process exits with
code 1 and the container is restarted. ``SIGTERM`` lets every loop finish the
pass it is in, then stops.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aiohttp import web

from app.bot.runner import _install_signal_handlers
from app.core.config import get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger
from app.core.resources import Resources
from app.daily_worker.health import Heartbeats, build_health_app
from app.daily_worker.loops import (
    EvaluatorLoop,
    ExpiryLoop,
    OutboxLoop,
    PlannerLoop,
    SenderLoop,
)
from app.domain.enums import DailyOutboxChannel
from app.infrastructure.telegram.daily_pacing import PacedDailySender, PacedTextSender, SendPacer
from app.infrastructure.telegram.daily_runtime import TelegramTextSender, TelegramTokenProbe
from app.infrastructure.telegram.factory import create_bot, create_daily_bot, create_daily_sender

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.config import Settings
    from app.daily_worker.loops import WorkerLoop

logger = get_logger(__name__)

SHUTDOWN_GRACE_SECONDS: Final = 25.0
"""How long the loops get to finish their pass after ``SIGTERM``."""


def worker_configuration_problems(settings: Settings) -> tuple[str, ...]:
    """What keeps the worker from starting; empty when nothing does."""
    daily = settings.daily
    if daily.bot_token is None:
        return ("DAILY_BOT_TOKEN is not set",)
    if daily.bot_token.get_secret_value() == settings.telegram.bot_token.get_secret_value():
        return ("DAILY_BOT_TOKEN must not be the shop bot's TELEGRAM_BOT_TOKEN",)
    return ()


@dataclass(frozen=True, slots=True)
class DailyWorker:
    """The loops of one worker process, and their heartbeats."""

    loops: tuple[WorkerLoop, ...]
    heartbeats: Heartbeats

    async def run(self, stop: asyncio.Event, *, grace_seconds: float) -> None:
        """Turn every loop until ``stop``; exit with code 1 if one ends on its own."""
        tasks = {
            asyncio.create_task(loop.run_forever(stop), name=f"daily-{loop.name}"): loop
            for loop in self.loops
        }
        stopping = asyncio.create_task(stop.wait(), name="daily-stop")
        done, _ = await asyncio.wait({*tasks, stopping}, return_when=asyncio.FIRST_COMPLETED)
        # Loops asked to stop end in the same instant as the request itself:
        # only one that ended while nobody asked has died.
        died = [] if stop.is_set() else [loop.name for task, loop in tasks.items() if task in done]
        stop.set()
        await self._finish(tasks, stopping, grace_seconds)
        if died:
            logger.critical("daily_worker_loop_died", loops=died)
            raise SystemExit(1)

    @staticmethod
    async def _finish(
        tasks: dict[asyncio.Task[None], WorkerLoop],
        stopping: asyncio.Task[bool],
        grace_seconds: float,
    ) -> None:
        """Let the loops finish their pass; cancel what is still running after it."""
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            logger.warning("daily_worker_loop_cancelled", loop=tasks[task].name)
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopping.cancel()
        with suppress(asyncio.CancelledError):
            await stopping


def create_daily_worker(
    container: Container,
    *,
    daily_bot: Bot,
    shop_bot: Bot,
) -> DailyWorker:
    """Every loop of the worker, sending through one budget."""
    settings = container.settings.daily
    pacer = SendPacer(rate_per_second=settings.send_rate_per_second)
    heartbeats = Heartbeats()
    delivery = container.build_daily_delivery(
        PacedDailySender(create_daily_sender(daily_bot), pacer),
    )
    outbox = container.build_daily_outbox(
        {
            DailyOutboxChannel.DAILY: PacedTextSender(TelegramTextSender(daily_bot), pacer),
            # Only for "Telegram refuses the Daily bot's token": a few messages a day.
            DailyOutboxChannel.SHOP: TelegramTextSender(shop_bot),
        }
    )
    return DailyWorker(
        loops=(
            PlannerLoop(
                planning=container.build_daily_planning(),
                settings=settings,
                heartbeats=heartbeats,
            ),
            SenderLoop(
                delivery=delivery,
                notices=container.build_daily_notices(),
                settings=settings,
                pacer=pacer,
                heartbeats=heartbeats,
            ),
            OutboxLoop(outbox=outbox, settings=settings, pacer=pacer, heartbeats=heartbeats),
            ExpiryLoop(
                delivery=delivery,
                outbox=outbox,
                settings=settings,
                heartbeats=heartbeats,
            ),
            EvaluatorLoop(
                stock=container.build_daily_stock(),
                alerts=container.build_daily_alerts(),
                probe=TelegramTokenProbe(daily_bot),
                settings=settings,
                heartbeats=heartbeats,
            ),
        ),
        heartbeats=heartbeats,
    )


async def main() -> None:
    """Check the configuration, build everything, run until ``SIGTERM``."""
    settings = get_settings()
    configure_logging(level=settings.app.log_level, log_format=settings.app.log_format)
    problems = worker_configuration_problems(settings)
    if problems:
        logger.error("daily_worker_not_configured", problems=list(problems))
        message = "The Daily worker is not configured: " + "; ".join(problems)
        raise SystemExit(message)

    resources = Resources.create(settings)
    container = Container.create(resources)
    daily_bot = create_daily_bot(settings.daily)
    shop_bot = create_bot(settings)
    worker = create_daily_worker(container, daily_bot=daily_bot, shop_bot=shop_bot)

    checks = await resources.check()
    logger.info("daily_worker_dependencies_checked", enabled=settings.daily.enabled, **checks)
    health = web.AppRunner(build_health_app(worker.heartbeats))
    await health.setup()
    site = web.TCPSite(health, host=settings.daily.worker_host, port=settings.daily.worker_port)
    await site.start()
    logger.info("daily_worker_started", port=settings.daily.worker_port)

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await worker.run(stop, grace_seconds=SHUTDOWN_GRACE_SECONDS)
    finally:
        await health.cleanup()
        await daily_bot.session.close()
        await shop_bot.session.close()
        await container.crypto_payments.close()
        await resources.close()
        logger.info("daily_worker_stopped")


def run() -> None:
    """Synchronous wrapper used by the container entrypoint."""
    with suppress(KeyboardInterrupt):
        asyncio.run(main())


__all__ = [
    "SHUTDOWN_GRACE_SECONDS",
    "DailyWorker",
    "create_daily_worker",
    "main",
    "run",
    "worker_configuration_problems",
]
