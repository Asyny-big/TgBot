"""SQLAlchemy models.

Database-level invariants are enforced here rather than only in Python: money
moves through this schema, so "one paid copy per user" and "an invoice id is
recorded once" are constraints, not conventions.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.slug import SLUG_MAX_LENGTH, SLUG_PATTERN
from app.infrastructure.db.base import Base, TimestampMixin

USDT_PRECISION: Final = 12
USDT_SCALE: Final = 2

_ACCESS_STATUS_SQL: Final = "status IN ('paid', 'delivered')"


def _enum_column[EnumT: enum.Enum](enum_class: type[EnumT], name: str) -> Enum:
    """Native PostgreSQL enum storing the enum *values* (not member names)."""
    return Enum(
        enum_class,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


class ProductModel(TimestampMixin, Base):
    """A digital good reachable through exactly one deep link."""

    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(SLUG_MAX_LENGTH), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_url: Mapped[str] = mapped_column(Text, nullable=False)
    price_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(USDT_PRECISION, USDT_SCALE),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    purchases: Mapped[list[PurchaseModel]] = relationship(
        back_populates="product",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_products_slug"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint(
            "price_stars IS NOT NULL OR price_usdt IS NOT NULL",
            name="price_present",
        ),
        CheckConstraint("price_stars IS NULL OR price_stars > 0", name="price_stars_positive"),
        CheckConstraint("price_usdt IS NULL OR price_usdt > 0", name="price_usdt_positive"),
        Index("ix_products_is_active_created_at", "is_active", "created_at"),
    )


class UserModel(Base):
    """A Telegram user who opened at least one product card."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    purchases: Mapped[list[PurchaseModel]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        Index("ix_users_username_lower", text("lower(username)")),
    )


class PurchaseModel(Base):
    """One attempt to buy one product through one provider."""

    __tablename__ = "purchases"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        _enum_column(PaymentProvider, "payment_provider"),
        nullable=False,
    )
    status: Mapped[PurchaseStatus] = mapped_column(
        _enum_column(PurchaseStatus, "purchase_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    amount: Mapped[Decimal] = mapped_column(nullable=False)
    currency: Mapped[Currency] = mapped_column(
        _enum_column(Currency, "currency"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_charge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[UserModel] = relationship(back_populates="purchases", lazy="raise")
    product: Mapped[ProductModel] = relationship(back_populates="purchases", lazy="raise")

    __table_args__ = (
        # One provider invoice maps to exactly one purchase: replayed webhooks
        # and duplicated Telegram updates cannot create a second row.
        UniqueConstraint("provider", "external_id", name="uq_purchases_provider_external_id"),
        # A user can hold at most one paid copy of a product. Enforced by the
        # database so a race between two payment callbacks cannot double-charge.
        Index(
            "uq_purchases_user_product_paid",
            "user_id",
            "product_id",
            unique=True,
            postgresql_where=text(_ACCESS_STATUS_SQL),
        ),
        Index(
            "uq_purchases_telegram_charge_id",
            "telegram_charge_id",
            unique=True,
            postgresql_where=text("telegram_charge_id IS NOT NULL"),
        ),
        Index("ix_purchases_paid_at", "paid_at"),
        Index("ix_purchases_created_at", "created_at"),
        Index("ix_purchases_product_id_status", "product_id", "status"),
        Index("ix_purchases_user_id", "user_id"),
        Index("ix_purchases_status_created_at", "status", "created_at"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'expired') OR paid_at IS NOT NULL",
            name="paid_at_present",
        ),
        CheckConstraint(
            "status <> 'delivered' OR (delivered_at IS NOT NULL AND delivered_url IS NOT NULL)",
            name="delivery_complete",
        ),
    )
