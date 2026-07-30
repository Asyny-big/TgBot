"""Trigram indexes for the admin panel's substring search.

Revision ID: 0002
Revises: 0001
Created: production audit

The admin panel searches with ``ILIKE '%term%'``, which no B-tree index can
serve: the pattern has no anchored prefix. Measured on 200 000 purchases,
2 000 products and 50 000 buyers, a search took 0.6-1.3 s because every query
scanned the whole join — including a search by an exact invoice id, which should
be a single index lookup.

``pg_trgm`` indexes the three-character sequences of a value, which lets
PostgreSQL answer an unanchored ``ILIKE`` from an index. It is a trusted
extension since PostgreSQL 13, so the database owner can create it without
superuser rights.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TRIGRAM_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_products_title_trgm", "products", "title"),
    ("ix_products_slug_trgm", "products", "slug"),
    ("ix_products_description_trgm", "products", "description"),
    ("ix_users_username_trgm", "users", "username"),
    ("ix_purchases_external_id_trgm", "purchases", "external_id"),
    ("ix_purchases_telegram_charge_id_trgm", "purchases", "telegram_charge_id"),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _TRIGRAM_INDEXES:
        op.create_index(
            name,
            table,
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for name, table, _ in reversed(_TRIGRAM_INDEXES):
        op.drop_index(name, table_name=table)
    # The extension is deliberately left installed. `CREATE EXTENSION IF NOT
    # EXISTS` is a no-op when an operator had already added it, so dropping it
    # here would remove something this migration never created.
