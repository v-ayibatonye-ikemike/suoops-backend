"""Live courier delivery status on storefront escrow

Revision ID: 20260712_delivery_status
Revises: 20260712_courier_delivery_gate
Create Date: 2026-07-12

Adds ``delivery_status`` (normalized courier status from the Shipbubble webhook)
and ``delivery_status_at`` to ``storefront_order_escrow`` so the buyer and seller
can see live progress — awaiting pickup → picked up → in transit → out for
delivery → delivered — instead of only a booked/delivered flag.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260712_delivery_status"
down_revision = "20260712_courier_delivery_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_status", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_status_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "delivery_status_at")
    op.drop_column("storefront_order_escrow", "delivery_status")
