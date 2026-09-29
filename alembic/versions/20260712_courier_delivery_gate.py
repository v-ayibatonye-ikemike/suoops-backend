"""Courier delivery + inspection timestamps on storefront escrow

Revision ID: 20260712_courier_delivery_gate
Revises: 20260711_delivery_service_type
Create Date: 2026-07-12

Adds ``delivery_booked_at`` and ``courier_delivered_at`` to
``storefront_order_escrow`` so payouts for courier-shipped orders can be gated on
actual delivery + a post-delivery inspection window (not the flat payment-time
window), and undelivered orders can be flagged for review instead of auto-paid.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260712_courier_delivery_gate"
down_revision = "20260711_delivery_service_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_booked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("courier_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "courier_delivered_at")
    op.drop_column("storefront_order_escrow", "delivery_booked_at")
