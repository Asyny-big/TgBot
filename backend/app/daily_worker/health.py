"""The Daily worker's liveness: every loop must keep turning.

Each loop beats after every iteration — whether the iteration succeeded or
failed. A loop that fails because PostgreSQL or Telegram is down is alive:
it will succeed again once they are back, and restarting the container would
only add a restart loop to an outage. What the check catches is a loop that
stopped turning — stuck, or dead — for longer than it ever should: then the
health route answers 503 and the container is restarted.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web

from app import __version__
from app.bot.webhooks import HEALTH_PATH

if TYPE_CHECKING:
    from collections.abc import Callable


class Heartbeats:
    """When each loop last turned, and how long it may stay silent."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[str, float] = {}
        self._within: dict[str, float] = {}

    def expect(self, name: str, *, within: float) -> None:
        """Watch a loop that must beat at least every ``within`` seconds."""
        self._within[name] = within
        self._last[name] = self._clock()

    def beat(self, name: str) -> None:
        """The loop finished an iteration, successfully or not."""
        self._last[name] = self._clock()

    def ages(self) -> dict[str, float]:
        """Seconds since each loop last turned."""
        now = self._clock()
        return {name: now - last for name, last in self._last.items()}

    def stale(self) -> tuple[str, ...]:
        """The loops silent for longer than they may be."""
        ages = self.ages()
        return tuple(sorted(name for name, age in ages.items() if age > self._within[name]))


HEARTBEATS_KEY: web.AppKey[Heartbeats] = web.AppKey("daily_worker_heartbeats")


async def handle_health(request: web.Request) -> web.Response:
    """200 while every loop turns; 503 naming the ones that stopped."""
    heartbeats = request.app[HEARTBEATS_KEY]
    stale = heartbeats.stale()
    body = {
        "status": "stale" if stale else "alive",
        "version": __version__,
        "loops": {name: round(age, 1) for name, age in sorted(heartbeats.ages().items())},
        "stale": list(stale),
    }
    return web.json_response(body, status=503 if stale else 200)


def build_health_app(heartbeats: Heartbeats) -> web.Application:
    """The worker's only HTTP surface: the container's health check."""
    app = web.Application()
    app[HEARTBEATS_KEY] = heartbeats
    app.router.add_get(HEALTH_PATH, handle_health)
    return app


__all__ = ["HEARTBEATS_KEY", "Heartbeats", "build_health_app", "handle_health"]
