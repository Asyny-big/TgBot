"""Initial schema: products, users, purchases.

Revision ID: 0001
Revises:
Create Date: 2026-07-30

Money-critical invariants live in the database, not only in Python:
* ``uq_purchases_provider_external_id`` — one provider invoice, one purchase.
* ``uq_purchases_user_product_paid`` — at most one paid copy per buyer.
* ``uq_purchases_telegram_charge_id`` — a Telegram charge is recorded once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_PROVIDER = "payment_provider"
PURCHASE_STATUS = "purchase_status"
CURRENCY = "currency"


def upgrade() -> None:
    """Create the initial schema."""
    op.create_table(
        "products",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("photo_file_id", sa.String(length=255), nullable=True),
        sa.Column("delivery_url", sa.Text(), nullable=False),
        sa.Column("price_stars", sa.Integer(), nullable=True),
        sa.Column("price_usdt", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[A-Za-z0-9_-]{1,64}$'",
            name=op.f("ck_products_slug_format"),
        ),
        sa.CheckConstraint(
            "price_stars IS NOT NULL OR price_usdt IS NOT NULL",
            name=op.f("ck_products_price_present"),
        ),
        sa.CheckConstraint(
            "price_stars IS NULL OR price_stars > 0",
            name=op.f("ck_products_price_stars_positive"),
        ),
        sa.CheckConstraint(
            "price_usdt IS NULL OR price_usdt > 0",
            name=op.f("ck_products_price_usdt_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("slug", name="uq_products_slug"),
    )
    op.create_index(
        "ix_products_is_active_created_at",
        "products",
        ["is_active", "created_at"],
        unique=False,
    )

    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("telegram_id > 0", name=op.f("ck_users_telegram_id_positive")),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_users")),
    )
    # Case-insensitive username lookups from the admin search box.
    op.create_index(
        "ix_users_username_lower",
        "users",
        [sa.literal_column("lower(username)")],
        unique=False,
    )

    op.create_table(
        "purchases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Enum("stars", "crypto", name=PAYMENT_PROVIDER), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "paid",
                "delivered",
                "refunded",
                "expired",
                name=PURCHASE_STATUS,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("currency", sa.Enum("XTR", "USDT", name=CURRENCY), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("telegram_charge_id", sa.String(length=128), nullable=True),
        sa.Column("delivered_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'delivered' OR (delivered_at IS NOT NULL AND delivered_url IS NOT NULL)",
            name=op.f("ck_purchases_delivery_complete"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'expired') OR paid_at IS NOT NULL",
            name=op.f("ck_purchases_paid_at_present"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_purchases_amount_positive")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_purchases_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_purchases_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchases")),
        sa.UniqueConstraint("provider", "external_id", name="uq_purchases_provider_external_id"),
    )
    op.create_index("ix_purchases_created_at", "purchases", ["created_at"], unique=False)
    op.create_index("ix_purchases_paid_at", "purchases", ["paid_at"], unique=False)
    op.create_index(
        "ix_purchases_product_id_status",
        "purchases",
        ["product_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_purchases_status_created_at",
        "purchases",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index("ix_purchases_user_id", "purchases", ["user_id"], unique=False)
    op.create_index(
        "uq_purchases_telegram_charge_id",
        "purchases",
        ["telegram_charge_id"],
        unique=True,
        postgresql_where=sa.text("telegram_charge_id IS NOT NULL"),
    )
    op.create_index(
        "uq_purchases_user_product_paid",
        "purchases",
        ["user_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('paid', 'delivered')"),
    )


def downgrade() -> None:
    """Drop the initial schema, including the enum types it created."""
    op.drop_index(
        "uq_purchases_user_product_paid",
        table_name="purchases",
        postgresql_where=sa.text("status IN ('paid', 'delivered')"),
    )
    op.drop_index(
        "uq_purchases_telegram_charge_id",
        table_name="purchases",
        postgresql_where=sa.text("telegram_charge_id IS NOT NULL"),
    )
    op.drop_index("ix_purchases_user_id", table_name="purchases")
    op.drop_index("ix_purchases_status_created_at", table_name="purchases")
    op.drop_index("ix_purchases_product_id_status", table_name="purchases")
    op.drop_index("ix_purchases_paid_at", table_name="purchases")
    op.drop_index("ix_purchases_created_at", table_name="purchases")
    op.drop_table("purchases")

    op.drop_index("ix_users_username_lower", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_products_is_active_created_at", table_name="products")
    op.drop_table("products")

    # Enum types outlive their tables in PostgreSQL, so drop them explicitly:
    # otherwise a second upgrade fails with "type already exists".
    for enum_name in (PURCHASE_STATUS, PAYMENT_PROVIDER, CURRENCY):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
