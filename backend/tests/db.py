"""Database fixtures for integration tests.

Each test runs inside a transaction that is rolled back afterwards, so the suite
is order independent and leaves no rows behind. Migrations are applied (and
rolled back) once per session, which also proves ``downgrade`` works.
"""

from __future__ import annotations

import os
from contextlib import suppress
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.core.config import BACKEND_DIR
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import PaymentProvider, PurchaseStatus
from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.domain.entities import Product, Purchase, User

DSN_ENV_VAR = "TEST_DATABASE_DSN"
ALEMBIC_DSN_ENV_VAR = "ALEMBIC_DATABASE_DSN"


def alembic_config(dsn: str) -> Config:
    """Alembic configuration pointing at the test database."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    os.environ[ALEMBIC_DSN_ENV_VAR] = dsn
    return config


@pytest.fixture(scope="session")
def database_dsn() -> str:
    """DSN of a disposable PostgreSQL database, or skip the integration tests."""
    dsn = os.getenv(DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{DSN_ENV_VAR} is not set — skipping database integration tests")
    return dsn


@pytest.fixture(scope="session")
def migrated_database(database_dsn: str) -> str:
    """Bring the test database to ``head`` from scratch."""
    config = alembic_config(database_dsn)
    with suppress(CommandError):  # a cold, empty database has nothing to undo
        command.downgrade(config, "base")
    command.upgrade(config, "head")
    return database_dsn


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """Session bound to a transaction that is always rolled back."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        # Repository commits become savepoints, so the outer rollback still wins.
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
def products(db_session: AsyncSession) -> SqlAlchemyProductRepository:
    return SqlAlchemyProductRepository(db_session)


@pytest.fixture
def users(db_session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(db_session)


@pytest.fixture
def purchases(db_session: AsyncSession) -> SqlAlchemyPurchaseRepository:
    return SqlAlchemyPurchaseRepository(db_session)


@pytest.fixture
def stats(db_session: AsyncSession) -> SqlAlchemyStatsRepository:
    return SqlAlchemyStatsRepository(db_session)


def product_draft(**overrides: Any) -> ProductDraft:
    """A valid product draft with a unique slug."""
    values: dict[str, Any] = {
        "slug": f"vip{uuid4().hex[:8]}",
        "title": "VIP access",
        "description": "Lifetime access to the VIP channel",
        "delivery_url": "https://t.me/+private-invite",
        "price_stars": 100,
        "price_usdt": Decimal("4.99"),
    }
    values.update(overrides)
    return ProductDraft(**values)


def user_draft(telegram_id: int, **overrides: Any) -> UserDraft:
    """A valid user draft."""
    values: dict[str, Any] = {
        "telegram_id": telegram_id,
        "username": f"buyer{telegram_id}",
        "first_name": "Buyer",
        "language_code": "ru",
    }
    values.update(overrides)
    return UserDraft(**values)


def purchase_draft(
    user: User,
    product: Product,
    provider: PaymentProvider = PaymentProvider.STARS,
    **overrides: Any,
) -> PurchaseDraft:
    """A pending purchase draft for the given user and product."""
    amount = Decimal(product.price_stars or 0)
    if provider is PaymentProvider.CRYPTO:
        amount = product.price_usdt or Decimal("1")
    values: dict[str, Any] = {
        "user_id": user.telegram_id,
        "product_id": product.id,
        "provider": provider,
        "amount": amount,
        "currency": provider.currency,
        "external_id": uuid4().hex,
    }
    values.update(overrides)
    return PurchaseDraft(**values)


async def seed_paid_purchase(
    purchase_repository: SqlAlchemyPurchaseRepository,
    user: User,
    product: Product,
    provider: PaymentProvider = PaymentProvider.STARS,
    **overrides: Any,
) -> Purchase:
    """Create a purchase and move it straight to ``paid``."""
    created = await purchase_repository.create(purchase_draft(user, product, provider, **overrides))
    return await purchase_repository.mark_paid(created.id)


__all__ = [
    "DSN_ENV_VAR",
    "PaymentProvider",
    "PurchaseStatus",
    "alembic_config",
    "product_draft",
    "purchase_draft",
    "seed_paid_purchase",
    "user_draft",
]
