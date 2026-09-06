"""Referral relationships and the internal bonus ledger.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06

Additive only. Two tables are created and five nullable-or-defaulted columns are
added; nothing is dropped, nothing is renamed and no existing row is rewritten.
The previous release therefore keeps running against this schema unchanged,
which is what makes "roll back the application, leave the database alone" a
valid recovery path.

The money-critical invariants of the new feature live here rather than only in
Python, for the same reason the purchase constraints do:

* ``uq_referrals_referred_user_id`` — one referrer per buyer, for life. A second
  invitation link cannot take a buyer over, however the two requests interleave.
* ``uq_referrals_discount_purchase_id`` — the one-time discount is consumed by
  at most one purchase.
* ``uq_bonus_transactions_purchase_type`` — one ledger entry of each kind per
  purchase. This is what makes a replayed payment notification credit a reward
  once instead of five times.
* ``ck_users_bonus_balance_non_negative`` — a balance cannot go below zero, so
  the same bonus units can never be spent twice.
* ``ck_purchases_amount_breakdown_consistent`` — a sale's stored breakdown must
  explain its own price.

``purchases.base_amount`` is deliberately left NULL on existing rows instead of
being backfilled from ``amount``. A backfill would mean a full-table UPDATE
holding a write lock over every historical sale, and it would buy nothing: for a
row written before this feature the billed amount *was* the list price, which is
exactly how the domain reads a NULL (``Purchase.list_amount``).

One note for a large deployment: ``ix_purchases_user_id_status`` is a plain
``CREATE INDEX`` and takes a lock that blocks writes to ``purchases`` while it
builds — the same caveat migration 0002 carries. On a table with millions of
sales, apply this during a quiet minute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BONUS_TRANSACTION_TYPE = "bonus_transaction_type"

_BONUS_TRANSACTION_TYPES = (
    "referral_reward",
    "bonus_reserved",
    "bonus_spent",
    "bonus_reserve_released",
)


def upgrade() -> None:
    """Create the referral and bonus schema, and extend users and purchases."""
    _extend_users()
    _extend_purchases()
    _create_referrals()
    _create_bonus_transactions()


def downgrade() -> None:
    """Remove everything this revision added.

    Safe only while the new tables hold no data worth keeping: dropping them
    discards the bonus ledger, and a credited reward cannot be reconstructed
    from anywhere else. Once bonuses have been accrued in production, roll back
    the application and leave this schema in place.
    """
    op.drop_index(
        "uq_bonus_transactions_purchase_type",
        table_name="bonus_transactions",
        postgresql_where=sa.text("purchase_id IS NOT NULL"),
    )
    op.drop_index("ix_bonus_transactions_user_id_created_at", table_name="bonus_transactions")
    op.drop_index("ix_bonus_transactions_referral_id", table_name="bonus_transactions")
    op.drop_table("bonus_transactions")

    op.drop_index(
        "uq_referrals_discount_purchase_id",
        table_name="referrals",
        postgresql_where=sa.text("discount_purchase_id IS NOT NULL"),
    )
    op.drop_index("ix_referrals_referrer_user_id", table_name="referrals")
    op.drop_index("ix_referrals_created_at", table_name="referrals")
    op.drop_table("referrals")

    # Bare names on purpose: the metadata naming convention turns
    # "amount_breakdown_consistent" into "ck_purchases_amount_breakdown_consistent"
    # on the way out, and passing the full name would prefix it twice.
    op.drop_constraint("amount_breakdown_consistent", "purchases", type_="check")
    op.drop_constraint("bonus_amount_non_negative", "purchases", type_="check")
    op.drop_constraint("discount_amount_non_negative", "purchases", type_="check")
    op.drop_index("ix_purchases_user_id_status", table_name="purchases")
    op.drop_column("purchases", "bonus_amount")
    op.drop_column("purchases", "discount_amount")
    op.drop_column("purchases", "base_amount")

    op.drop_constraint("bonus_balance_non_negative", "users", type_="check")
    op.drop_constraint("uq_users_referral_code", "users", type_="unique")
    op.drop_column("users", "bonus_balance")
    op.drop_column("users", "referral_code")

    # A PostgreSQL enum outlives the table that used it, so a second upgrade
    # would fail with "type already exists" unless it is dropped explicitly.
    sa.Enum(name=BONUS_TRANSACTION_TYPE).drop(op.get_bind(), checkfirst=True)


def _extend_users() -> None:
    """Add the referral code and the cached bonus balance."""
    op.add_column("users", sa.Column("referral_code", sa.String(length=16), nullable=True))
    op.add_column(
        "users",
        sa.Column("bonus_balance", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint("uq_users_referral_code", "users", ["referral_code"])
    op.create_check_constraint("bonus_balance_non_negative", "users", "bonus_balance >= 0")


def _extend_purchases() -> None:
    """Record how a billed amount was reached, without touching ``amount``."""
    op.add_column(
        "purchases",
        sa.Column("base_amount", sa.Numeric(precision=18, scale=6), nullable=True),
    )
    op.add_column(
        "purchases",
        sa.Column(
            "discount_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "purchases",
        sa.Column(
            "bonus_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "discount_amount_non_negative",
        "purchases",
        "discount_amount >= 0",
    )
    op.create_check_constraint("bonus_amount_non_negative", "purchases", "bonus_amount >= 0")
    # Existing rows satisfy this because their base_amount is NULL.
    op.create_check_constraint(
        "amount_breakdown_consistent",
        "purchases",
        "base_amount IS NULL OR amount = base_amount - discount_amount - bonus_amount",
    )
    # Referral eligibility asks "has this buyer ever bought anything?" on every
    # checkout, which is a lookup by user and status.
    op.create_index("ix_purchases_user_id_status", "purchases", ["user_id", "status"], unique=False)


def _create_referrals() -> None:
    """One buyer, permanently attributed to whoever invited them."""
    op.create_table(
        "referrals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("referrer_user_id", sa.BigInteger(), nullable=False),
        sa.Column("referred_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("discount_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discount_purchase_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(discount_used_at IS NULL) = (discount_purchase_id IS NULL)",
            name=op.f("ck_referrals_discount_usage_complete"),
        ),
        sa.CheckConstraint(
            "referrer_user_id <> referred_user_id",
            name=op.f("ck_referrals_no_self_referral"),
        ),
        sa.ForeignKeyConstraint(
            ["discount_purchase_id"],
            ["purchases.id"],
            name=op.f("fk_referrals_discount_purchase_id_purchases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referred_user_id"],
            ["users.telegram_id"],
            name=op.f("fk_referrals_referred_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referrer_user_id"],
            ["users.telegram_id"],
            name=op.f("fk_referrals_referrer_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referrals")),
        sa.UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
    )
    op.create_index("ix_referrals_created_at", "referrals", ["created_at"], unique=False)
    op.create_index(
        "ix_referrals_referrer_user_id",
        "referrals",
        ["referrer_user_id"],
        unique=False,
    )
    op.create_index(
        "uq_referrals_discount_purchase_id",
        "referrals",
        ["discount_purchase_id"],
        unique=True,
        postgresql_where=sa.text("discount_purchase_id IS NOT NULL"),
    )


def _create_bonus_transactions() -> None:
    """The bonus ledger: every movement of every balance, with its reason."""
    op.create_table(
        "bonus_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(*_BONUS_TRANSACTION_TYPES, name=BONUS_TRANSACTION_TYPE),
            nullable=False,
        ),
        sa.Column("referral_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_id", sa.Uuid(), nullable=True),
        sa.Column("stars_per_usdt", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_bonus_transactions_amount_not_zero")),
        sa.CheckConstraint(
            "stars_per_usdt IS NULL OR stars_per_usdt > 0",
            name=op.f("ck_bonus_transactions_rate_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_id"],
            ["purchases.id"],
            name=op.f("fk_bonus_transactions_purchase_id_purchases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referral_id"],
            ["referrals.id"],
            name=op.f("fk_bonus_transactions_referral_id_referrals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_bonus_transactions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bonus_transactions")),
    )
    op.create_index(
        "ix_bonus_transactions_referral_id",
        "bonus_transactions",
        ["referral_id"],
        unique=False,
    )
    op.create_index(
        "ix_bonus_transactions_user_id_created_at",
        "bonus_transactions",
        ["user_id", "created_at"],
        unique=False,
    )
    # The idempotency guarantee of the whole feature.
    op.create_index(
        "uq_bonus_transactions_purchase_type",
        "bonus_transactions",
        ["purchase_id", "type"],
        unique=True,
        postgresql_where=sa.text("purchase_id IS NOT NULL"),
    )
