"""Daily Content runtime: the outbox and the owner's alerts.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-11

Schema only, and additive: two new tables and their enum types. Nothing that
exists is touched, so the previous release runs unchanged against it.

* ``daily_outbox`` — every message the Daily runtime owes somebody that is not
  a library item: the owner's alerts, a buyer's "you have seen everything".
  ``uq_daily_outbox_dedupe_key`` makes queueing idempotent: the same alert or
  notice is queued once, however many workers decide it at once. Sending
  follows the deliveries' discipline — a lease while an attempt is under way,
  retries for uncertain failures, a deadline after which it is dropped.
* ``daily_alerts`` — one row per alert: its state, how many notifications it
  has queued, and what it last observed. Locked while evaluated, so two
  workers never notify the same transition twice.

The message kind and the channel are plain text (the channel checked): later
kinds need no ``ALTER TYPE``.

**The downgrade refuses rather than destroys** while either table holds a row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "daily_outbox_status": ("pending", "sending", "sent", "failed", "expired"),
    "daily_alert_state": ("ok", "firing"),
}

_TABLES = ("daily_outbox", "daily_alerts")

OUTBOX_LEASE_SHAPE = (
    "(status = 'sending' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) "
    "OR (status <> 'sending' AND lease_token IS NULL AND lease_until IS NULL)"
)
OUTBOX_SENT_SHAPE = (
    "(status = 'sent' AND sent_at IS NOT NULL) "
    "OR (status <> 'sent' AND sent_at IS NULL AND telegram_message_id IS NULL)"
)
OUTBOX_CHANNEL = "channel IN ('daily', 'shop')"
OUTBOX_BODY_LENGTH = "char_length(body) BETWEEN 1 AND 4096"
OUTBOX_DUE = "status = 'pending'"
OUTBOX_OPEN = "status IN ('pending', 'sending')"
ALERT_FIRING_SHAPE = "state <> 'firing' OR fired_at IS NOT NULL"


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
    """Create the outbox and the alerts."""
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)
    _create_outbox()
    _create_alerts()


def downgrade() -> None:
    """Drop them, refusing while anything has been recorded in them."""
    if not context.is_offline_mode():
        _refuse_to_erase_history()
    for table in _TABLES:
        op.drop_table(table)
    bind = op.get_bind()
    for name in reversed(tuple(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _create_outbox() -> None:
    op.create_table(
        "daily_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            _enum("daily_outbox_status"),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(OUTBOX_LEASE_SHAPE, name=op.f("ck_daily_outbox_lease_shape")),
        sa.CheckConstraint(OUTBOX_SENT_SHAPE, name=op.f("ck_daily_outbox_sent_shape")),
        sa.CheckConstraint(OUTBOX_CHANNEL, name=op.f("ck_daily_outbox_channel_known")),
        sa.CheckConstraint(OUTBOX_BODY_LENGTH, name=op.f("ck_daily_outbox_body_length")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_daily_outbox_attempts_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_outbox")),
        sa.UniqueConstraint("dedupe_key", name="uq_daily_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_daily_outbox_due",
        "daily_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text(OUTBOX_DUE),
    )
    op.create_index(
        "ix_daily_outbox_lease_until",
        "daily_outbox",
        ["lease_until"],
        postgresql_where=sa.text("status = 'sending'"),
    )
    op.create_index(
        "ix_daily_outbox_open_deadline",
        "daily_outbox",
        ["deadline_at"],
        postgresql_where=sa.text(OUTBOX_OPEN),
    )


def _create_alerts() -> None:
    op.create_table(
        "daily_alerts",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            _enum("daily_alert_state"),
            server_default=sa.text("'ok'"),
            nullable=False,
        ),
        sa.Column("notifications", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(ALERT_FIRING_SHAPE, name=op.f("ck_daily_alerts_firing_shape")),
        sa.CheckConstraint(
            "notifications >= 0",
            name=op.f("ck_daily_alerts_notifications_non_negative"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_daily_alerts")),
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
            "Refusing to downgrade the Daily Content runtime schema: the database holds "
            + "; ".join(found)
            + ". The outbox and the alerts are the record of what the owner and the "
            "buyers were told."
        )
        raise RuntimeError(message)
