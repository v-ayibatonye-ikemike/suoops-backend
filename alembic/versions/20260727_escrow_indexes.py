"""Composite indexes on storefront_order_escrow (status,id) + (status,release_due_at)

Revision ID: 20260727_escrow_indexes
Revises: 20260723_category_pack_price
Create Date: 2026-07-27

Speeds up the Trust & Safety dispute queue (filter by status, order by id desc,
paginated) and the auto-release worker (held orders due for settlement) so both
stay index-only at scale instead of scanning the whole escrow table.

NOTE: revision id kept <=32 chars — alembic_version.version_num is varchar(32).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260727_escrow_indexes"
down_revision = "20260723_category_pack_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_escrow_status_id",
        "storefront_order_escrow",
        ["status", "id"],
    )
    op.create_index(
        "ix_escrow_status_release",
        "storefront_order_escrow",
        ["status", "release_due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_escrow_status_release", table_name="storefront_order_escrow")
    op.drop_index("ix_escrow_status_id", table_name="storefront_order_escrow")
