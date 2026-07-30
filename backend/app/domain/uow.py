"""Unit of work contract.

One use case equals one transaction. Services receive a *factory* rather than a
session, so a scenario that has to talk to Telegram between two state changes
(confirm payment → send link → mark delivered) never holds a database
transaction open across a network call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from app.domain.repositories import (
        ProductRepository,
        PurchaseRepository,
        StatsRepository,
        UserRepository,
    )


class UnitOfWork(Protocol):
    """Transactional scope exposing every repository."""

    @property
    def products(self) -> ProductRepository:
        """Product repository bound to this transaction."""
        ...

    @property
    def users(self) -> UserRepository:
        """User repository bound to this transaction."""
        ...

    @property
    def purchases(self) -> PurchaseRepository:
        """Purchase repository bound to this transaction."""
        ...

    @property
    def stats(self) -> StatsRepository:
        """Statistics repository bound to this transaction."""
        ...

    async def commit(self) -> None:
        """Persist everything done in this scope."""
        ...

    async def rollback(self) -> None:
        """Discard everything done in this scope."""
        ...


class UnitOfWorkFactory(Protocol):
    """Opens a fresh transactional scope.

    The scope commits when the block exits normally and rolls back on any
    exception, so a service body cannot leave a half-applied transaction.
    """

    def __call__(self) -> AbstractAsyncContextManager[UnitOfWork]:
        """Return an async context manager yielding a unit of work."""
        ...


__all__ = ["UnitOfWork", "UnitOfWorkFactory"]
