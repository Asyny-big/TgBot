"""SQLAlchemy unit of work."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.infrastructure.db.engine import Database


class SqlAlchemyUnitOfWork:
    """Repositories sharing one session, and therefore one transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._products = SqlAlchemyProductRepository(session)
        self._users = SqlAlchemyUserRepository(session)
        self._purchases = SqlAlchemyPurchaseRepository(session)
        self._stats = SqlAlchemyStatsRepository(session)

    @property
    def session(self) -> AsyncSession:
        return self._session

    @property
    def products(self) -> SqlAlchemyProductRepository:
        return self._products

    @property
    def users(self) -> SqlAlchemyUserRepository:
        return self._users

    @property
    def purchases(self) -> SqlAlchemyPurchaseRepository:
        return self._purchases

    @property
    def stats(self) -> SqlAlchemyStatsRepository:
        return self._stats

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    """Opens one transaction per use case."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyUnitOfWork]:
        """Yield a unit of work; commit on success, roll back on any error."""
        async with self._database.session_factory() as session:
            unit = SqlAlchemyUnitOfWork(session)
            try:
                yield unit
            except Exception:
                await session.rollback()
                raise
            await session.commit()
