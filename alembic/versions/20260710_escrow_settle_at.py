"""T+1 settlement gate for storefront escrow payouts

Revision ID: 20260710_escrow_settle_at
Revises: 20260710_transfer_provider
Create Date: 2026-07-10

Adds ``storefront_order_escrow.settle_at`` — the earliest time a released hold
may actually pay out. Sellers settle on a T+1 cadence (the daily settlement run
after Flutterwave's own T+1 settlement lands), so payouts come from settled
collections rather than float, and no seller is paid the same day.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_escrow_settle_at"
down_revision = "20260710_transfer_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("settle_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_storefront_order_escrow_settle_at", "storefront_order_escrow", ["settle_at"])


def downgrade() -> None:
    op.drop_index("ix_storefront_order_escrow_settle_at", table_name="storefront_order_escrow")
    op.drop_column("storefront_order_escrow", "settle_at")
