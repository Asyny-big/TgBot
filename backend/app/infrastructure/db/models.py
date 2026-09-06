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

from app.domain.enums import (
    BonusTransactionType,
    Currency,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.referrals import REFERRAL_CODE_MAX_LENGTH
from app.domain.slug import SLUG_MAX_LENGTH, SLUG_PATTERN
from app.infrastructure.db.base import Base, TimestampMixin

USDT_PRECISION: Final = 12
USDT_SCALE: Final = 2

USDT_RATE_PRECISION: Final = 18
USDT_RATE_SCALE: Final = 6
"""Width of the recorded bonus conversion rate. Wide enough to never round it."""

_ACCESS_STATUS_SQL: Final = "status IN ('paid', 'delivered')"

#: Operator class that makes ``ILIKE '%term%'`` index-assisted.
_TRGM_OPS: Final = "gin_trgm_ops"


def _trigram_index(name: str, column: str) -> Index:
    """A GIN trigram index for the admin panel's substring search.

    A B-tree cannot serve ``ILIKE '%term%'`` — the pattern has no anchored
    prefix — so without this the admin search degrades into a full table scan as
    soon as the shop has real traffic.
    """
    return Index(name, column, postgresql_using="gin", postgresql_ops={column: _TRGM_OPS})


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
        _trigram_index("ix_products_title_trgm", "title"),
        _trigram_index("ix_products_slug_trgm", "slug"),
        _trigram_index("ix_products_description_trgm", "description"),
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

    referral_code: Mapped[str | None] = mapped_column(
        String(REFERRAL_CODE_MAX_LENGTH),
        nullable=True,
    )
    # Cache of the bonus ledger's sum. Written in the same transaction as the
    # entry that moved it, so the two cannot drift apart.
    bonus_balance: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )

    purchases: Mapped[list[PurchaseModel]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        # A referral code identifies exactly one inviter.
        UniqueConstraint("referral_code", name="uq_users_referral_code"),
        # The last line of defence against spending bonuses twice: whatever
        # races upstream, a balance cannot go negative.
        CheckConstraint("bonus_balance >= 0", name="bonus_balance_non_negative"),
        Index("ix_users_username_lower", text("lower(username)")),
        _trigram_index("ix_users_username_trgm", "username"),
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
    """What the buyer is billed. Unchanged by the referral feature."""

    # How ``amount`` was reached. Nullable ``base_amount`` is what keeps the
    # migration additive: rows written before this feature have no breakdown,
    # and for those the billed amount *was* the list price.
    base_amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    bonus_amount: Mapped[Decimal] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
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
        Index("ix_purchases_user_id_status", "user_id", "status"),
        _trigram_index("ix_purchases_external_id_trgm", "external_id"),
        _trigram_index("ix_purchases_telegram_charge_id_trgm", "telegram_charge_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("discount_amount >= 0", name="discount_amount_non_negative"),
        CheckConstraint("bonus_amount >= 0", name="bonus_amount_non_negative"),
        # The breakdown must add up. Without this a bug in the pricing code
        # could store a sale whose history does not explain its own price.
        CheckConstraint(
            "base_amount IS NULL OR amount = base_amount - discount_amount - bonus_amount",
            name="amount_breakdown_consistent",
        ),
        CheckConstraint(
            "status IN ('pending', 'expired') OR paid_at IS NOT NULL",
            name="paid_at_present",
        ),
        CheckConstraint(
            "status <> 'delivered' OR (delivered_at IS NOT NULL AND delivered_url IS NOT NULL)",
            name="delivery_complete",
        ),
    )


class ReferralModel(Base):
    """One buyer, permanently attributed to whoever invited them."""

    __tablename__ = "referrals"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    referrer_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    referred_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    # The first-purchase discount exists once. These two columns are what burn
    # it, and they are set when the payment is confirmed rather than when the
    # invoice is issued — an abandoned invoice must not consume the discount.
    discount_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    discount_purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # One referrer per buyer, for life. A later link cannot take the buyer
        # over, and this is the constraint that guarantees it rather than a
        # check in Python that a race could slip past.
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
        CheckConstraint("referrer_user_id <> referred_user_id", name="no_self_referral"),
        CheckConstraint(
            "(discount_used_at IS NULL) = (discount_purchase_id IS NULL)",
            name="discount_usage_complete",
        ),
        # A discount is consumed by at most one purchase.
        Index(
            "uq_referrals_discount_purchase_id",
            "discount_purchase_id",
            unique=True,
            postgresql_where=text("discount_purchase_id IS NOT NULL"),
        ),
        Index("ix_referrals_referrer_user_id", "referrer_user_id"),
        Index("ix_referrals_created_at", "created_at"),
    )


class BonusTransactionModel(Base):
    """One movement of the internal bonus balance.

    An append-only ledger, with one deliberate exception: a reservation is
    promoted from ``bonus_reserved`` to ``bonus_spent`` when the invoice behind
    it is paid. The amount never changes — only the reason the units are held.
    """

    __tablename__ = "bonus_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Signed bonus units: positive credits the balance, negative debits it."""

    type: Mapped[BonusTransactionType] = mapped_column(
        _enum_column(BonusTransactionType, "bonus_transaction_type"),
        nullable=False,
    )
    referral_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("referrals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rate_units_per_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(USDT_RATE_PRECISION, USDT_RATE_SCALE),
        nullable=True,
    )
    """Rate in force when a USDT amount was converted, so history stays readable."""

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        # The idempotency guarantee for the whole feature: one entry of each
        # kind per purchase. Five replayed payment notifications credit one
        # reward, not five, because the second insert cannot exist.
        Index(
            "uq_bonus_transactions_purchase_type",
            "purchase_id",
            "type",
            unique=True,
            postgresql_where=text("purchase_id IS NOT NULL"),
        ),
        CheckConstraint("amount <> 0", name="amount_not_zero"),
        CheckConstraint(
            "rate_units_per_usdt IS NULL OR rate_units_per_usdt > 0",
            name="rate_positive",
        ),
        Index("ix_bonus_transactions_user_id_created_at", "user_id", "created_at"),
        Index("ix_bonus_transactions_referral_id", "referral_id"),
    )
