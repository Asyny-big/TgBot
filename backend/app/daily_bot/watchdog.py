"""The watchdog's loop in the Daily bot's process.

One check every ``DAILY_WATCHDOG_INTERVAL_SECONDS``, next to the webhook or
the long polling. A failing check — Redis away for a moment — is logged and
the loop goes on: the watchdog must never take the bot down with it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.services.daily_watchdog import DailyWatchdog

logger = get_logger(__name__)


async def run_watchdog(watchdog: DailyWatchdog, stop: asyncio.Event, *, interval: float) -> None:
    """Check the worker until ``stop`` is set; survive every failure of a check."""
    logger.info("daily_watchdog_started", interval=interval)
    while not stop.is_set():
        try:
            await watchdog.check()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # the bot must outlive any watchdog failure
            logger.error(  # noqa: TRY400 — one failed check must not stop the loop
                "daily_watchdog_check_failed",
                error=str(error),
                error_type=type(error).__name__,
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    logger.info("daily_watchdog_stopped")


__all__ = ["run_watchdog"]
