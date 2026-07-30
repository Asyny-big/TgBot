"""Alembic environment running on the async engine."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings
from app.infrastructure.db import models  # noqa: F401  (imported for metadata side effect)
from app.infrastructure.db.base import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

DSN_ENV_VAR = "ALEMBIC_DATABASE_DSN"

target_metadata = Base.metadata


def database_dsn() -> str:
    """DSN for migrations: explicit override first, validated settings second."""
    override = os.getenv(DSN_ENV_VAR)
    if override:
        return override
    return get_settings().postgres.dsn


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``)."""
    context.configure(
        url=database_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already established synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Advisory lock: two containers starting at once cannot race migrations.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('tgshop_migrations'))")
        context.run_migrations()


async def run_migrations_online() -> None:
    """Create an async engine and run every pending migration."""
    engine = create_async_engine(database_dsn(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
