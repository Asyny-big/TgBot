"""One worker's sending budget: an even pace, and the pause after a 429."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from app.core.exceptions import DailySendBlockedError, DailySendTransientError
from app.infrastructure.telegram.daily_pacing import PacedDailySender, PacedTextSender, SendPacer

if TYPE_CHECKING:
    from app.domain.entities import DailyContent


@dataclass
class FakeTime:
    """A monotonic clock that only moves when somebody sleeps on it."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


def _pacer(rate: float = 20) -> tuple[SendPacer, FakeTime]:
    time = FakeTime()
    return SendPacer(rate_per_second=rate, clock=time, sleep=time.sleep), time


async def _ok() -> int:
    return 1


def _too_fast(seconds: float) -> DailySendTransientError:
    return DailySendTransientError("Telegram flood control", retry_after=seconds)


async def test_sends_are_spaced_evenly() -> None:
    pacer, time = _pacer(rate=20)

    for _ in range(5):
        await pacer.run(_ok)

    assert time.slept == [0.05, 0.05, 0.05, 0.05]


async def test_concurrent_sends_queue_up_in_order() -> None:
    """Each takes its slot before waiting: nobody wakes at the same time."""
    pacer, time = _pacer(rate=10)
    started: list[float] = []

    async def send() -> int:
        started.append(time.now)
        return 1

    await asyncio.gather(*(pacer.run(send) for _ in range(3)))

    assert started == pytest.approx([1000.0, 1000.1, 1000.2])


async def test_idle_time_is_not_saved_up_as_a_burst() -> None:
    pacer, time = _pacer(rate=20)
    await pacer.run(_ok)
    time.now += 60  # a quiet minute

    await pacer.run(_ok)
    await pacer.run(_ok)

    assert time.slept == [0.05]


async def test_a_429_pauses_every_send_of_the_worker() -> None:
    pacer, time = _pacer()
    calls = 0

    async def flooded() -> int:
        raise _too_fast(30)

    async def counted() -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(DailySendTransientError):
        await pacer.run(flooded)
    assert pacer.paused_for() == pytest.approx(30)

    time.now += 10
    with pytest.raises(DailySendTransientError) as held:
        await pacer.run(counted)
    assert calls == 0  # never reached Telegram
    assert held.value.retry_after == pytest.approx(20)

    time.now += 20
    assert pacer.paused_for() == 0
    assert await pacer.run(counted) == 1
    assert calls == 1


async def test_a_pause_is_never_shortened() -> None:
    pacer, _ = _pacer()
    pacer.pause(60)
    pacer.pause(5)

    assert pacer.paused_for() == pytest.approx(60)


async def test_a_429_arriving_while_a_send_waits_for_its_turn_holds_it_too() -> None:
    pacer, time = _pacer(rate=1)
    await pacer.run(_ok)  # the next slot is a second away

    async def sleep_into_a_pause(seconds: float) -> None:
        time.now += seconds
        pacer.pause(30)  # another send got a 429 meanwhile

    pacer._sleep = sleep_into_a_pause
    with pytest.raises(DailySendTransientError):
        await pacer.run(_ok)


async def test_other_failures_do_not_pause() -> None:
    pacer, _ = _pacer()

    async def blocked() -> int:
        message = "Forbidden: bot was blocked by the user"
        raise DailySendBlockedError(message)

    async def no_hint() -> int:
        message = "Telegram is unavailable"
        raise DailySendTransientError(message)

    for failure in (blocked, no_hint):
        with pytest.raises((DailySendBlockedError, DailySendTransientError)):
            await pacer.run(failure)
    assert pacer.paused_for() == 0


def test_a_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        SendPacer(rate_per_second=0)


@dataclass
class _Inner:
    sent: list[tuple[int, object]] = field(default_factory=list)
    failure: Exception | None = None

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        del protect_content
        if self.failure is not None:
            raise self.failure
        self.sent.append((chat_id, content))
        return 7

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.failure is not None:
            raise self.failure
        self.sent.append((chat_id, text))
        return 8


async def test_items_and_messages_share_one_budget() -> None:
    pacer, time = _pacer(rate=20)
    inner = _Inner()
    items = PacedDailySender(inner, pacer)
    messages = PacedTextSender(inner, pacer)

    await items.send(1, object(), protect_content=True)  # type: ignore[arg-type]
    await messages.send_text(2, "hi")
    await items.send(3, object(), protect_content=True)  # type: ignore[arg-type]

    assert [chat for chat, _ in inner.sent] == [1, 2, 3]
    assert time.slept == [0.05, 0.05]


async def test_a_429_on_an_item_holds_the_messages_too() -> None:
    pacer, _ = _pacer()
    inner = _Inner(failure=_too_fast(15))
    with pytest.raises(DailySendTransientError):
        await PacedDailySender(inner, pacer).send(1, object(), protect_content=True)  # type: ignore[arg-type]

    inner.failure = None
    with pytest.raises(DailySendTransientError):
        await PacedTextSender(inner, pacer).send_text(2, "alert")
    assert inner.sent == []
