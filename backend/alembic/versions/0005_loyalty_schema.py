"""Loyalty programme schema: internal grants, waived amounts and rewards.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08

Schema only. The loyalty behaviour is not implemented yet and is switched off
by ``LOYALTY_ENABLED``; with it off nothing writes to anything added here, and
the shop behaves exactly as before.

What is added:

* ``payment_provider`` gains ``'internal'``. Not a payment rail: it marks a pack
  the shop hands over itself (a loyalty free pack), with no invoice and no money.
* ``purchases.loyalty_amount`` — what a loyalty reward waived. ``NOT NULL
  DEFAULT 0`` is a catalogue-only change on PostgreSQL 11+: no row is rewritten.
* ``ck_purchases_amount_matches_provider`` replaces ``amount > 0``: a payment
  rail always bills something, an internal grant bills exactly nothing.
* ``ck_purchases_internal_is_loyalty_grant`` — an internal purchase waives its
  whole list price, carries no other reduction and no Telegram charge, and is
  never pending or expired, so no payment provider is ever asked about it.
* ``ck_purchases_amount_breakdown_consistent`` now subtracts ``loyalty_amount``.
  Every existing row still satisfies it: their ``loyalty_amount`` is 0.
* ``loyalty_rewards`` — one row per buyer and milestone, written lazily the
  first time a reward is used. No backfill: nothing is written here today.

**One transaction, on purpose.** ``env.py`` runs every migration inside one
transaction holding an advisory lock, and ``ALTER TYPE … ADD VALUE`` is allowed
inside a transaction on PostgreSQL 12+ — and rolled back with it on failure.
What is *not* allowed is using the new value before it is committed ("unsafe use
of new value"). That is why every check below compares ``provider::text``
instead of the enum literal. ``autocommit_block`` would avoid the restriction
but end the transaction, and with it the advisory lock.

**The downgrade is real, and refuses rather than destroys.** PostgreSQL cannot
drop one value from an enum, so the type is rebuilt without it. That is only
possible — and only honest — while no history depends on what is being removed:
an internal purchase is a pack a buyer received, a ``loyalty_amount`` is part of
a sale's price breakdown, and an applied or consumed reward is a promise that
was kept. If any of those exist the downgrade raises and changes nothing.
Rewards still ``available`` hold no history and are dropped: they would be
recorded again from the buyer's purchases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import context, op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_PROVIDER = "payment_provider"
REWARD_KIND = "loyalty_reward_kind"
REWARD_STATUS = "loyalty_reward_status"

_REWARD_KINDS = ("discount_50_percent", "free_pack")
_REWARD_STATUSES = ("available", "applied", "consumed")

# Migration 0004's index: its predicate compares ``provider`` with an enum
# literal, so it has to be rebuilt around the type swap in the downgrade.
PENDING_CRYPTO_INDEX = "uq_purchases_user_product_pending_crypto"
PENDING_CRYPTO_PREDICATE = "status = 'pending' AND provider = 'crypto'"

AMOUNT_MATCHES_PROVIDER = (
    "(provider::text = 'internal' AND amount = 0) OR (provider::text <> 'internal' AND amount > 0)"
)
INTERNAL_IS_LOYALTY_GRANT = (
    "provider::text <> 'internal' OR ("
    "base_amount IS NOT NULL AND base_amount > 0 "
    "AND loyalty_amount = base_amount "
    "AND discount_amount = 0 AND bonus_amount = 0 "
    "AND telegram_charge_id IS NULL "
    "AND status::text IN ('paid', 'delivered', 'refunded'))"
)
BREAKDOWN_WITH_LOYALTY = (
    "base_amount IS NULL OR amount = base_amount - discount_amount - loyalty_amount - bonus_amount"
)
BREAKDOWN_WITHOUT_LOYALTY = (
    "base_amount IS NULL OR amount = base_amount - discount_amount - bonus_amount"
)
REWARD_MILESTONE_KIND = (
    "(milestone = 5 AND kind::text = 'discount_50_percent') "
    "OR (milestone = 10 AND kind::text = 'free_pack')"
)
REWARD_STATE_SHAPE = (
    "(status::text = 'available' AND applied_purchase_id IS NULL AND applied_at IS NULL "
    "AND consumed_purchase_id IS NULL AND consumed_at IS NULL) "
    "OR (status::text = 'applied' AND applied_purchase_id IS NOT NULL "
    "AND applied_at IS NOT NULL AND consumed_purchase_id IS NULL AND consumed_at IS NULL) "
    "OR (status::text = 'consumed' AND consumed_purchase_id IS NOT NULL "
    "AND consumed_at IS NOT NULL)"
)
REWARD_FREE_PACK_NEVER_APPLIED = "kind::text <> 'free_pack' OR status::text <> 'applied'"

# What a downgrade would erase or contradict. Each is a reason to refuse.
_DOWNGRADE_BLOCKERS = (
    (
        "internal purchase(s) — packs handed over by the loyalty programme",
        "SELECT count(*) FROM purchases WHERE provider::text = 'internal'",
    ),
    (
        "purchase(s) with a loyalty_amount — part of their recorded price",
        "SELECT count(*) FROM purchases WHERE loyalty_amount > 0",
    ),
    (
        "applied or consumed loyalty reward(s)",
        "SELECT count(*) FROM loyalty_rewards WHERE status::text <> 'available'",
    ),
)


def upgrade() -> None:
    """Add the internal provider, the waived amount, its checks and the rewards table."""
    op.execute(f"ALTER TYPE {PAYMENT_PROVIDER} ADD VALUE IF NOT EXISTS 'internal'")

    op.add_column(
        "purchases",
        sa.Column(
            "loyalty_amount",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint("loyalty_amount_non_negative", "purchases", "loyalty_amount >= 0")

    op.drop_constraint("amount_breakdown_consistent", "purchases", type_="check")
    op.create_check_constraint("amount_breakdown_consistent", "purchases", BREAKDOWN_WITH_LOYALTY)

    op.drop_constraint("amount_positive", "purchases", type_="check")
    op.create_check_constraint("amount_matches_provider", "purchases", AMOUNT_MATCHES_PROVIDER)
    op.create_check_constraint("internal_is_loyalty_grant", "purchases", INTERNAL_IS_LOYALTY_GRANT)

    _create_loyalty_rewards()


def downgrade() -> None:
    """Remove the loyalty schema, refusing if anything recorded depends on it."""
    if not context.is_offline_mode():
        _refuse_to_erase_history()

    op.drop_index("uq_loyalty_rewards_consumed_purchase_id", table_name="loyalty_rewards")
    op.drop_index("uq_loyalty_rewards_applied_purchase_id", table_name="loyalty_rewards")
    op.drop_table("loyalty_rewards")
    sa.Enum(name=REWARD_STATUS).drop(op.get_bind(), checkfirst=True)
    sa.Enum(name=REWARD_KIND).drop(op.get_bind(), checkfirst=True)

    op.drop_constraint("internal_is_loyalty_grant", "purchases", type_="check")
    op.drop_constraint("amount_matches_provider", "purchases", type_="check")
    op.create_check_constraint("amount_positive", "purchases", "amount > 0")

    op.drop_constraint("amount_breakdown_consistent", "purchases", type_="check")
    op.create_check_constraint(
        "amount_breakdown_consistent", "purchases", BREAKDOWN_WITHOUT_LOYALTY
    )
    op.drop_constraint("loyalty_amount_non_negative", "purchases", type_="check")
    op.drop_column("purchases", "loyalty_amount")

    _rebuild_payment_provider_without_internal()


def _create_loyalty_rewards() -> None:
    op.create_table(
        "loyalty_rewards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("milestone", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.Enum(*_REWARD_KINDS, name=REWARD_KIND), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_REWARD_STATUSES, name=REWARD_STATUS),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
        sa.Column("applied_purchase_id", sa.Uuid(), nullable=True),
        sa.Column("consumed_purchase_id", sa.Uuid(), nullable=True),
        sa.Column(
            "earned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(REWARD_MILESTONE_KIND, name=op.f("ck_loyalty_rewards_milestone_kind")),
        sa.CheckConstraint(REWARD_STATE_SHAPE, name=op.f("ck_loyalty_rewards_state_shape")),
        sa.CheckConstraint(
            REWARD_FREE_PACK_NEVER_APPLIED,
            name=op.f("ck_loyalty_rewards_free_pack_never_applied"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_loyalty_rewards_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["applied_purchase_id"],
            ["purchases.id"],
            name=op.f("fk_loyalty_rewards_applied_purchase_id_purchases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_purchase_id"],
            ["purchases.id"],
            name=op.f("fk_loyalty_rewards_consumed_purchase_id_purchases"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loyalty_rewards")),
        sa.UniqueConstraint("user_id", "milestone", name="uq_loyalty_rewards_user_milestone"),
    )
    op.create_index(
        "uq_loyalty_rewards_applied_purchase_id",
        "loyalty_rewards",
        ["applied_purchase_id"],
        unique=True,
        postgresql_where=sa.text("applied_purchase_id IS NOT NULL"),
    )
    op.create_index(
        "uq_loyalty_rewards_consumed_purchase_id",
        "loyalty_rewards",
        ["consumed_purchase_id"],
        unique=True,
        postgresql_where=sa.text("consumed_purchase_id IS NOT NULL"),
    )


def _refuse_to_erase_history() -> None:
    bind = op.get_bind()
    found = [
        f"{count} {what}"
        for what, query in _DOWNGRADE_BLOCKERS
        if (count := bind.execute(sa.text(query)).scalar_one())
    ]
    if found:
        message = (
            "Refusing to downgrade the loyalty schema: the database holds "
            + "; ".join(found)
            + ". Removing it would erase or contradict recorded history."
        )
        raise RuntimeError(message)


def _rebuild_payment_provider_without_internal() -> None:
    """Swap the enum for one without 'internal'; PostgreSQL cannot drop a value.

    Migration 0004's partial index compares ``provider`` with an enum literal,
    so PostgreSQL cannot carry it across the type change ("operator does not
    exist"). It is dropped first and rebuilt identically afterwards. The unique
    constraint on (provider, external_id) is rebuilt by PostgreSQL itself.
    """
    op.drop_index(PENDING_CRYPTO_INDEX, table_name="purchases")
    op.execute(f"ALTER TYPE {PAYMENT_PROVIDER} RENAME TO {PAYMENT_PROVIDER}_with_internal")
    op.execute(f"CREATE TYPE {PAYMENT_PROVIDER} AS ENUM ('stars', 'crypto')")
    op.execute(
        f"ALTER TABLE purchases ALTER COLUMN provider TYPE {PAYMENT_PROVIDER} "
        f"USING provider::text::{PAYMENT_PROVIDER}"
    )
    op.execute(f"DROP TYPE {PAYMENT_PROVIDER}_with_internal")
    op.create_index(
        PENDING_CRYPTO_INDEX,
        "purchases",
        ["user_id", "product_id"],
        unique=True,
        postgresql_where=sa.text(PENDING_CRYPTO_PREDICATE),
    )
