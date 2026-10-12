"""At most one live CryptoBot checkout per buyer and product.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08

One partial unique index, nothing else: no column, no constraint on existing
rows, no data rewritten. The previous release keeps running against this schema
unchanged — it simply could not record a second pending crypto invoice for the
same buyer and product any more, which is the point.

The checkout already holds a per-buyer, per-product lock across the whole
"look for an invoice → create one → record it" sequence. This index is the
part that still holds if that lock ever expired mid-checkout: the second
insert is refused by PostgreSQL, the application reports it as a checkout
already in progress, and the invoice that lost the race is never shown to the
buyer, so it expires unpaid.

The upgrade refuses to run while duplicates exist rather than resolving them.
Expiring one of them here could expire an invoice the buyer is about to pay;
waiting is always safe, because every pending invoice expires on its own within
``CRYPTOBOT_INVOICE_TTL_SECONDS`` and housekeeping then closes it.

Like 0002 and 0003 this is a plain ``CREATE INDEX``: it blocks writes to
``purchases`` while it builds. The predicate only matches pending crypto rows,
so the build is a scan of the table and an index of a handful of entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import context, op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_purchases_user_product_pending_crypto"
PREDICATE = "status = 'pending' AND provider = 'crypto'"

_DUPLICATES_SQL = f"""
SELECT count(*) FROM (
    SELECT 1 FROM purchases
    WHERE {PREDICATE}
    GROUP BY user_id, product_id
    HAVING count(*) > 1
) AS duplicated
"""  # noqa: S608 — a module constant, nothing interpolated from input


def upgrade() -> None:
    """Create the index, refusing to touch data if duplicates already exist.

    ``alembic upgrade --sql`` has no database to ask, so the check is online
    only; a script generated that way fails on the index itself instead.
    """
    if not context.is_offline_mode():
        _refuse_duplicates()

    op.create_index(
        INDEX_NAME,
        "purchases",
        ["user_id", "product_id"],
        unique=True,
        postgresql_where=sa.text(PREDICATE),
    )


def _refuse_duplicates() -> None:
    duplicated = op.get_bind().execute(sa.text(_DUPLICATES_SQL)).scalar_one()
    if duplicated:
        message = (
            f"{duplicated} buyer/product pair(s) still hold more than one pending "
            "crypto purchase. They expire on their own within the invoice TTL; run "
            "this migration again once housekeeping has expired them."
        )
        raise RuntimeError(message)


def downgrade() -> None:
    """Drop the index. No data depends on it."""
    op.drop_index(INDEX_NAME, table_name="purchases", postgresql_where=sa.text(PREDICATE))
