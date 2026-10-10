"""Deterministic races for tests over the live database.

A race between two transactions is decided by timing; a test that waits for
the right timing passes or fails by luck. A ``Gate`` makes the dangerous order
happen on purpose instead: every party is held right after a chosen repository
call, until all of them have made it. When the code under test serialises the
parties (a row lock, an advisory lock, a unique index), they can never all
arrive — and after ``patience`` the gate gives up and lets them through one by
one, which is exactly the safe order.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


class Gate:
    """Holds each party until all ``parties`` arrived, or ``patience`` ran out."""

    def __init__(self, parties: int, *, patience: float = 0.3) -> None:
        self._parties = parties
        self._arrived = 0
        self._open = asyncio.Event()
        self._patience = patience

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._open.set()
        try:
            await asyncio.wait_for(self._open.wait(), self._patience)
        except TimeoutError:
            self._open.set()


class _GatedRepository:
    def __init__(self, inner: Any, method: str, gate: Gate) -> None:
        self._inner = inner
        self._method = method
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name != self._method:
            return attribute

        async def held(*args: Any, **kwargs: Any) -> Any:
            result = await attribute(*args, **kwargs)
            await self._gate.wait()
            return result

        return held


class _GatedUnitOfWork:
    def __init__(self, inner: Any, repository: str, method: str, gate: Gate) -> None:
        self._inner = inner
        self._repository = repository
        self._method = method
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name != self._repository:
            return attribute
        return _GatedRepository(attribute, self._method, self._gate)


def gated(factory: Callable[[], Any], gate: Gate, *, repository: str, method: str) -> Any:
    """``factory``, with every ``uow.<repository>.<method>(...)`` held at ``gate``.

    The call completes first — inside its transaction, uncommitted — and only
    then waits: the other parties run up to the same point meanwhile.
    """

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[_GatedUnitOfWork]:
        async with factory() as uow:
            yield _GatedUnitOfWork(uow, repository, method, gate)

    return unit_of_work


async def warm_pool(factory: Callable[[], Any], connections: int) -> None:
    """Open the pool's connections up front, so parallel work really starts together.

    Otherwise the first party runs on the one open connection while the others
    are still connecting, and finishes before they begin.
    """

    async def hold() -> None:
        async with factory() as uow:
            await uow.session.execute(text("SELECT 1"))
            await asyncio.sleep(0.05)

    await asyncio.gather(*(hold() for _ in range(connections)))


async def all_at_once(*calls: Awaitable[Any]) -> list[Any]:
    """Run ``calls`` concurrently; every one is awaited even when one fails.

    Nothing is left running into the next test. Returns the results, with
    exceptions in place of the calls that raised.
    """
    return list(await asyncio.gather(*calls, return_exceptions=True))


__all__ = ["Gate", "all_at_once", "gated", "warm_pool"]
