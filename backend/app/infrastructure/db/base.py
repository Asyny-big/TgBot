"""Declarative base and shared column conventions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Final
from uuid import UUID

from sqlalchemy import DateTime, MetaData, Numeric, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names keep Alembic migrations reviewable: without a
# convention PostgreSQL invents names and every autogenerate diff churns.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MONEY_PRECISION: Final = 18
MONEY_SCALE: Final = 6


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        Decimal: Numeric(MONEY_PRECISION, MONEY_SCALE),
        UUID: Uuid(as_uuid=True),
    }


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
