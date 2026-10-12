"""Daily Content management: buyers' content requests, and the panel's outbox.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11

Additive: three new tables, one new enum type, and nullable columns on two
Daily tables. The previous release runs unchanged against it.

* ``daily_requests`` — a buyer's suggestion. ``uq_daily_requests_client_draft_id``
  makes submitting idempotent: the same draft sent twice — a double press, a
  retry after a network failure — is one request. ``version`` grows with every
  change from the admin panel, so a change made from a stale page is refused.
* ``daily_request_items`` — the request's messages, in order.
* ``daily_request_events`` — its audit trail; ``idempotency_key`` makes the
  owner's reply idempotent: the same reply submitted twice is queued once.
* ``daily_subscriptions.requests_banned_at/by`` — a buyer barred from sending
  requests, and who did it.
* ``daily_outbox.content_id`` / ``request_item_id`` — "send it to me" from the
  panel: a library item or a buyer's attachment, sent to the owner by the
  worker — the panel never holds the Daily bot's token.

**The downgrade refuses rather than destroys** while any of it holds data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("new", "in_review", "completed", "rejected")

REQUEST_CLOSED_SHAPE = "(status IN ('completed', 'rejected')) = (closed_at IS NOT NULL)"
REQUEST_NOTE_LENGTH = "admin_note IS NULL OR char_length(admin_note) <= 2000"
ITEM_KIND_SHAPE = (
    "(kind = 'text' AND file_id IS NULL AND body IS NOT NULL) "
    "OR (kind <> 'text' AND file_id IS NOT NULL)"
)
ITEM_BODY_LENGTH = "body IS NULL OR char_length(body) BETWEEN 1 AND 4096"
SUBSCRIPTION_BAN_SHAPE = "(requests_banned_at IS NULL) = (requests_banned_by IS NULL)"
OUTBOX_ONE_ATTACHMENT = "num_nonnulls(content_id, request_item_id) <= 1"

_REFUSE_WHILE = (
    ("daily_requests", "SELECT count(*) FROM daily_requests"),
    ("daily_request_events", "SELECT count(*) FROM daily_request_events"),
    (
        "daily_outbox (sends from the panel)",
        (
            "SELECT count(*) FROM daily_outbox "
            "WHERE content_id IS NOT NULL OR request_item_id IS NOT NULL"
        ),
    ),
    (
        "daily_subscriptions (request bans)",
        "SELECT count(*) FROM daily_subscriptions WHERE requests_banned_at IS NOT NULL",
    ),
)


def _status(name: str = "daily_request_status") -> postgresql.ENUM:
    return postgresql.ENUM(*_STATUSES, name=name, create_type=False)


def upgrade() -> None:
    """Create the requests, and give the outbox and the subscriptions their columns."""
    postgresql.ENUM(*_STATUSES, name="daily_request_status").create(
        op.get_bind(),
        checkfirst=False,
    )
    _create_requests()
    _create_items()
    _create_events()
    _extend_subscriptions()
    _extend_outbox()


def downgrade() -> None:
    """Drop it all, refusing while any of it holds data."""
    if not context.is_offline_mode():
        _refuse_to_erase_history()
    op.drop_constraint(op.f("ck_daily_outbox_one_attachment"), "daily_outbox", type_="check")
    op.drop_constraint(
        op.f("fk_daily_outbox_request_item_id_daily_request_items"),
        "daily_outbox",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_daily_outbox_content_id_daily_content"),
        "daily_outbox",
        type_="foreignkey",
    )
    op.drop_column("daily_outbox", "request_item_id")
    op.drop_column("daily_outbox", "content_id")
    op.drop_constraint(
        op.f("ck_daily_subscriptions_requests_ban_shape"),
        "daily_subscriptions",
        type_="check",
    )
    op.drop_column("daily_subscriptions", "requests_banned_by")
    op.drop_column("daily_subscriptions", "requests_banned_at")
    for table in ("daily_request_events", "daily_request_items", "daily_requests"):
        op.drop_table(table)
    postgresql.ENUM(name="daily_request_status").drop(op.get_bind(), checkfirst=True)


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


def _create_requests() -> None:
    op.create_table(
        "daily_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", _status(), server_default=sa.text("'new'"), nullable=False),
        sa.Column("client_draft_id", sa.Uuid(), nullable=False),
        sa.Column("purchases_at_submit", sa.Integer(), nullable=False),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(REQUEST_CLOSED_SHAPE, name=op.f("ck_daily_requests_closed_shape")),
        sa.CheckConstraint(REQUEST_NOTE_LENGTH, name=op.f("ck_daily_requests_note_length")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_daily_requests_version_positive")),
        sa.CheckConstraint(
            "purchases_at_submit >= 0",
            name=op.f("ck_daily_requests_purchases_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["daily_subscriptions.user_id"],
            name=op.f("fk_daily_requests_user_id_daily_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_requests")),
        sa.UniqueConstraint("client_draft_id", name="uq_daily_requests_client_draft_id"),
    )
    op.create_index(
        "ix_daily_requests_status_created_at",
        "daily_requests",
        ["status", "created_at"],
    )
    op.create_index("ix_daily_requests_user_id_status", "daily_requests", ["user_id", "status"])


def _create_items() -> None:
    op.create_table(
        "daily_request_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="daily_content_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("file_unique_id", sa.String(length=128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(ITEM_KIND_SHAPE, name=op.f("ck_daily_request_items_kind_shape")),
        sa.CheckConstraint(ITEM_BODY_LENGTH, name=op.f("ck_daily_request_items_body_length")),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 10",
            name=op.f("ck_daily_request_items_position_range"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["daily_requests.id"],
            name=op.f("fk_daily_request_items_request_id_daily_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_request_items")),
        sa.UniqueConstraint(
            "request_id",
            "position",
            name="uq_daily_request_items_request_position",
        ),
    )


def _create_events() -> None:
    op.create_table(
        "daily_request_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("from_status", _status(), nullable=True),
        sa.Column("to_status", _status(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("outbox_message_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["daily_requests.id"],
            name=op.f("fk_daily_request_events_request_id_daily_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_message_id"],
            ["daily_outbox.id"],
            name=op.f("fk_daily_request_events_outbox_message_id_daily_outbox"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_request_events")),
        sa.UniqueConstraint("idempotency_key", name="uq_daily_request_events_idempotency_key"),
    )
    op.create_index(
        "ix_daily_request_events_request_id",
        "daily_request_events",
        ["request_id"],
    )
    op.create_index(
        "ix_daily_request_events_outbox_message_id",
        "daily_request_events",
        ["outbox_message_id"],
    )


def _extend_subscriptions() -> None:
    op.add_column(
        "daily_subscriptions",
        sa.Column("requests_banned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "daily_subscriptions",
        sa.Column("requests_banned_by", sa.String(length=100), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_daily_subscriptions_requests_ban_shape"),
        "daily_subscriptions",
        SUBSCRIPTION_BAN_SHAPE,
    )


def _extend_outbox() -> None:
    op.add_column("daily_outbox", sa.Column("content_id", sa.BigInteger(), nullable=True))
    op.add_column("daily_outbox", sa.Column("request_item_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_daily_outbox_content_id_daily_content"),
        "daily_outbox",
        "daily_content",
        ["content_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_daily_outbox_request_item_id_daily_request_items"),
        "daily_outbox",
        "daily_request_items",
        ["request_item_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_daily_outbox_one_attachment"),
        "daily_outbox",
        OUTBOX_ONE_ATTACHMENT,
    )


def _refuse_to_erase_history() -> None:
    bind = op.get_bind()
    found = [
        f"{count} row(s) in {what}"
        for what, statement in _REFUSE_WHILE
        if (count := bind.execute(sa.text(statement)).scalar_one())
    ]
    if found:
        message = (
            "Refusing to downgrade the Daily Content requests schema: the database holds "
            + "; ".join(found)
            + ". Buyers' requests and the owner's answers exist nowhere else."
        )
        raise RuntimeError(message)
