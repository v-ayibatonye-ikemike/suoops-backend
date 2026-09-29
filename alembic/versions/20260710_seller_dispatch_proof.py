"""seller dispatch (send-out) proof on storefront escrow

Revision ID: 20260710_seller_dispatch
Revises: 20260710_order_messaging
Create Date: 2026-07-10

Adds seller-side DISPATCH proof to ``storefront_order_escrow`` — recorded when
the seller sends the package out (before delivery). Symmetric to buyer
protection: the seller logs the send-out time, a courier/waybill tracking code,
an optional note and a photo of the packaged item. Forms an auditable handoff
chain (dispatch -> delivery -> release) and lets the buyer see the order is on
its way.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_seller_dispatch"
down_revision = "20260710_order_messaging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("seller_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("dispatch_tracking", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("dispatch_note", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("dispatch_proof_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "dispatch_proof_url")
    op.drop_column("storefront_order_escrow", "dispatch_note")
    op.drop_column("storefront_order_escrow", "dispatch_tracking")
    op.drop_column("storefront_order_escrow", "seller_dispatched_at")
