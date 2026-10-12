"""Daily Content core: subscriptions, the library, deliveries and their attempts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10

Schema only, and additive: four new tables and their enum types. Nothing that
exists is touched, so the previous release runs unchanged against it. Daily
Content is switched off by ``DAILY_ENABLED``; with it off nothing writes here.

What the database guarantees from here on:

* ``uq_daily_deliveries_user_date`` — one delivery per buyer and Moscow date,
  however many planners, workers or restarts there are;
* ``uq_daily_deliveries_user_content`` — a buyer never holds the same item in
  two open deliveries, and never receives it again once it was sent;
* ``uq_daily_content_source`` — one Telegram message is one library item, even
  when its update reaches the bot twice;
* ``uq_daily_content_file_unique_id`` — the same file is in the library once
  while it is not archived;
* state-shape checks — every status carries exactly the facts it implies, and a
  lease exists exactly while a delivery is being sent.

Every enum type is new and created here, so — unlike in 0005 — its literals
are safe to use in the checks and index predicates of the same transaction.

**The downgrade refuses rather than destroys.** Library items are the owner's
work and deliveries are every buyer's history — the very thing that stops an
item from being sent twice. If any row exists, the downgrade raises and changes
nothing; on an empty schema it drops the tables and the types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "daily_subscription_status": ("inactive", "active", "paused"),
    "daily_content_status": ("active", "disabled", "archived"),
    "daily_content_kind": (
        "text",
        "photo",
        "video",
        "animation",
        "document",
        "audio",
        "voice",
        "video_note",
    ),
    "daily_delivery_trigger": ("activation", "schedule"),
    "daily_delivery_status": (
        "pending",
        "sending",
        "sent",
        "no_content",
        "failed",
        "expired",
        "cancelled",
    ),
    "daily_attempt_outcome": (
        "sent",
        "transient",
        "permanent",
        "blocked",
        "content_invalid",
        "lease_lost",
        "crashed",
    ),
}

_TABLES = (
    "daily_delivery_attempts",
    "daily_deliveries",
    "daily_content",
    "daily_subscriptions",
)
"""In dependency order: each table only references the ones after it."""

SUBSCRIPTION_SHAPE = (
    "(status = 'inactive' AND activated_at IS NULL AND anchor_minute IS NULL "
    "AND paused_at IS NULL) "
    "OR (status = 'active' AND activated_at IS NOT NULL AND anchor_minute IS NOT NULL "
    "AND paused_at IS NULL) "
    "OR (status = 'paused' AND activated_at IS NOT NULL AND anchor_minute IS NOT NULL "
    "AND paused_at IS NOT NULL)"
)
ANCHOR_RANGE = "anchor_minute IS NULL OR (anchor_minute >= 0 AND anchor_minute < 1440)"
ACTIVATION_ORDER = "activated_at IS NULL OR activated_at >= first_interaction_at"
SUBSCRIPTION_DELIVERABLE = "status = 'active' AND blocked_at IS NULL"

CONTENT_SHAPE = (
    "(kind = 'text' AND file_id IS NULL AND file_unique_id IS NULL AND body IS NOT NULL) "
    "OR (kind <> 'text' AND file_id IS NOT NULL AND file_unique_id IS NOT NULL)"
)
CONTENT_BODY_LENGTH = "body IS NULL OR char_length(body) BETWEEN 1 AND 4096"
CONTENT_SIZES = (
    "(file_size IS NULL OR file_size >= 0) AND (duration IS NULL OR duration >= 0) "
    "AND (width IS NULL OR width >= 0) AND (height IS NULL OR height >= 0)"
)
CONTENT_FILE_LIVE = "file_unique_id IS NOT NULL AND status <> 'archived'"

DELIVERY_SENT_SHAPE = (
    "(status = 'sent' AND content_id IS NOT NULL AND sent_at IS NOT NULL "
    "AND telegram_message_id IS NOT NULL) "
    "OR (status <> 'sent' AND sent_at IS NULL AND telegram_message_id IS NULL)"
)
DELIVERY_LEASE_SHAPE = (
    "(status = 'sending' AND content_id IS NOT NULL AND lease_token IS NOT NULL "
    "AND lease_until IS NOT NULL) "
    "OR (status <> 'sending' AND lease_token IS NULL AND lease_until IS NULL)"
)
DELIVERY_NO_CONTENT = "status <> 'no_content' OR content_id IS NULL"
DELIVERY_WINDOW_ORDER = (
    "window_opens_at <= scheduled_for AND scheduled_for <= window_closes_at "
    "AND window_closes_at <= deadline_at"
)
DELIVERY_HOLDS_CONTENT = "content_id IS NOT NULL AND status IN ('pending', 'sending', 'sent')"
DELIVERY_DUE = "status IN ('pending', 'no_content')"

ATTEMPT_SHAPE = "(finished_at IS NULL) = (outcome IS NULL)"


def _enum(name: str) -> postgresql.ENUM:
    """A column type for an enum created up front: never re-created by a table."""
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    """Create the Daily Content types and tables."""
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)

    _create_subscriptions()
    _create_content()
    _create_deliveries()
    _create_attempts()


def downgrade() -> None:
    """Drop Daily Content, refusing while anything has been recorded in it."""
    if not context.is_offline_mode():
        _refuse_to_erase_history()
    for table in _TABLES:
        op.drop_table(table)
    bind = op.get_bind()
    for name in reversed(tuple(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _create_subscriptions() -> None:
    op.create_table(
        "daily_subscriptions",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "status",
            _enum("daily_subscription_status"),
            server_default=sa.text("'inactive'"),
            nullable=False,
        ),
        sa.Column("first_interaction_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anchor_minute", sa.SmallInteger(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exhausted_notified_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(SUBSCRIPTION_SHAPE, name=op.f("ck_daily_subscriptions_state_shape")),
        sa.CheckConstraint(ANCHOR_RANGE, name=op.f("ck_daily_subscriptions_anchor_minute_range")),
        sa.CheckConstraint(
            ACTIVATION_ORDER,
            name=op.f("ck_daily_subscriptions_activation_after_first_interaction"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_daily_subscriptions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_daily_subscriptions")),
    )
    op.create_index(
        "ix_daily_subscriptions_deliverable_anchor",
        "daily_subscriptions",
        ["anchor_minute"],
        postgresql_where=sa.text(SUBSCRIPTION_DELIVERABLE),
    )


def _create_content() -> None:
    op.create_table(
        "daily_content",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "status",
            _enum("daily_content_status"),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("kind", _enum("daily_content_kind"), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("file_unique_id", sa.String(length=128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(CONTENT_SHAPE, name=op.f("ck_daily_content_kind_shape")),
        sa.CheckConstraint(CONTENT_BODY_LENGTH, name=op.f("ck_daily_content_body_length")),
        sa.CheckConstraint(CONTENT_SIZES, name=op.f("ck_daily_content_sizes_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_content")),
        sa.UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            name="uq_daily_content_source",
        ),
    )
    op.create_index(
        "uq_daily_content_file_unique_id",
        "daily_content",
        ["file_unique_id"],
        unique=True,
        postgresql_where=sa.text(CONTENT_FILE_LIVE),
    )
    op.create_index(
        "ix_daily_content_status_created_at",
        "daily_content",
        ["status", "created_at"],
    )


def _create_deliveries() -> None:
    op.create_table(
        "daily_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("trigger", _enum("daily_delivery_trigger"), nullable=False),
        sa.Column(
            "status",
            _enum("daily_delivery_status"),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("content_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_outcome", _enum("daily_attempt_outcome"), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(DELIVERY_SENT_SHAPE, name=op.f("ck_daily_deliveries_sent_shape")),
        sa.CheckConstraint(DELIVERY_LEASE_SHAPE, name=op.f("ck_daily_deliveries_lease_shape")),
        sa.CheckConstraint(
            DELIVERY_NO_CONTENT,
            name=op.f("ck_daily_deliveries_no_content_shape"),
        ),
        sa.CheckConstraint(
            DELIVERY_WINDOW_ORDER,
            name=op.f("ck_daily_deliveries_window_order"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_daily_deliveries_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["daily_subscriptions.user_id"],
            name=op.f("fk_daily_deliveries_user_id_daily_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["daily_content.id"],
            name=op.f("fk_daily_deliveries_content_id_daily_content"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_deliveries")),
        sa.UniqueConstraint("user_id", "delivery_date", name="uq_daily_deliveries_user_date"),
    )
    op.create_index(
        "uq_daily_deliveries_user_content",
        "daily_deliveries",
        ["user_id", "content_id"],
        unique=True,
        postgresql_where=sa.text(DELIVERY_HOLDS_CONTENT),
    )
    op.create_index(
        "ix_daily_deliveries_due",
        "daily_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text(DELIVERY_DUE),
    )
    op.create_index(
        "ix_daily_deliveries_lease_until",
        "daily_deliveries",
        ["lease_until"],
        postgresql_where=sa.text("status = 'sending'"),
    )
    op.create_index(
        "ix_daily_deliveries_user_id_status",
        "daily_deliveries",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_daily_deliveries_content_id_sent",
        "daily_deliveries",
        ["content_id"],
        postgresql_where=sa.text("status = 'sent'"),
    )
    op.create_index("ix_daily_deliveries_sent_at", "daily_deliveries", ["sent_at"])
    op.create_index(
        "ix_daily_deliveries_delivery_date_status",
        "daily_deliveries",
        ["delivery_date", "status"],
    )


def _create_attempts() -> None:
    op.create_table(
        "daily_delivery_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("delivery_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.SmallInteger(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", _enum("daily_attempt_outcome"), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name=op.f("ck_daily_delivery_attempts_attempt_no_positive"),
        ),
        sa.CheckConstraint(ATTEMPT_SHAPE, name=op.f("ck_daily_delivery_attempts_outcome_shape")),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["daily_deliveries.id"],
            name=op.f("fk_daily_delivery_attempts_delivery_id_daily_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["daily_content.id"],
            name=op.f("fk_daily_delivery_attempts_content_id_daily_content"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_delivery_attempts")),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_no",
            name="uq_daily_delivery_attempts_delivery_attempt",
        ),
        sa.UniqueConstraint("lease_token", name="uq_daily_delivery_attempts_lease_token"),
    )
    op.create_index(
        "ix_daily_delivery_attempts_finished_at",
        "daily_delivery_attempts",
        ["finished_at"],
    )


def _refuse_to_erase_history() -> None:
    bind = op.get_bind()
    found = [
        f"{count} row(s) in {table}"
        for table in _TABLES
        if (count := bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608 — fixed table names
    ]
    if found:
        message = (
            "Refusing to downgrade the Daily Content schema: the database holds "
            + "; ".join(found)
            + ". The library and the delivery history exist nowhere else."
        )
        raise RuntimeError(message)
