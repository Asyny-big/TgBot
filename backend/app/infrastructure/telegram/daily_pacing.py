"""One Daily worker's sending budget: an even pace and Telegram's flood control.

Telegram limits how fast a bot may send — about thirty messages a second over
all chats — and answers 429 with ``retry_after`` once a bot goes faster.
Flood control applies to the whole bot, so after one 429 every further
request only prolongs the ban. ``SendPacer`` therefore does two things for
everything one worker sends, items and messages alike:

* spaces the sends evenly, at most ``rate_per_second``;
* after a 429, pauses the whole worker for ``retry_after``: a send asked for
  meanwhile is not attempted at all — it fails at once as a transient error
  carrying the remaining pause, so the engine schedules it again for when the
  pause is over, exactly as if Telegram had answered 429 itself.

The pacer belongs to one process. Two workers would each keep their own pace:
nothing breaks — every guarantee against duplicates lives in the database —
but together they may go faster, and a 429 would slow them down again.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from app.core.exceptions import DailySendTransientError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.domain.daily import DailyContentSender, DailyTextSender
    from app.domain.entities import DailyContent

logger = get_logger(__name__)


class SendPacer:
    """Even spacing between sends, and a pause after Telegram's 429."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            message = f"rate_per_second must be positive, got {rate_per_second}"
            raise ValueError(message)
        self._interval = 1 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._paused_until = 0.0

    def paused_for(self) -> float:
        """Seconds left of the flood-control pause; ``0`` when there is none."""
        return max(self._paused_until - self._clock(), 0.0)

    def pause(self, seconds: float) -> None:
        """Hold every send of this worker for ``seconds`` (never shortened)."""
        until = self._clock() + seconds
        if until > self._paused_until:
            self._paused_until = until
            logger.warning("daily_send_paused", seconds=round(seconds, 1))

    async def acquire(self) -> None:
        """Wait for this send's turn: one every ``1 / rate`` seconds.

        The slot is taken before waiting, so concurrent senders queue up in
        order instead of all waking at once.
        """
        now = self._clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self._interval
        if slot > now:
            await self._sleep(slot - now)

    async def run(self, send: Callable[[], Awaitable[int]]) -> int:
        """Run one send within the budget; learn from its 429."""
        self._refuse_while_paused()
        await self.acquire()
        # A 429 may have arrived while this send waited for its turn.
        self._refuse_while_paused()
        try:
            return await send()
        except DailySendTransientError as error:
            if error.retry_after:
                self.pause(error.retry_after)
            raise

    def _refuse_while_paused(self) -> None:
        remaining = self.paused_for()
        if remaining > 0:
            message = f"Telegram flood control: paused for another {remaining:.0f}s"
            raise DailySendTransientError(message, retry_after=remaining)


class PacedDailySender:
    """A ``DailyContentSender`` that keeps to the worker's budget."""

    def __init__(self, inner: DailyContentSender, pacer: SendPacer) -> None:
        self._inner = inner
        self._pacer = pacer

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        return await self._pacer.run(
            lambda: self._inner.send(chat_id, content, protect_content=protect_content)
        )


class PacedTextSender:
    """A ``DailyTextSender`` that keeps to the same budget as the items."""

    def __init__(self, inner: DailyTextSender, pacer: SendPacer) -> None:
        self._inner = inner
        self._pacer = pacer

    async def send_text(self, chat_id: int, text: str) -> int:
        return await self._pacer.run(lambda: self._inner.send_text(chat_id, text))


__all__ = ["PacedDailySender", "PacedTextSender", "SendPacer"]
