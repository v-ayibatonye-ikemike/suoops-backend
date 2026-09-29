"""escrow charge_reference (for buyer refunds)

Revision ID: 20260708_escrow_charge_ref
Revises: 20260708_escrow_recipient
Create Date: 2026-07-08

Adds ``storefront_order_escrow.charge_reference`` — the original Paystack charge
reference (INVPAY-…) captured at payment time, used to refund the buyer on a
valid non-delivery dispute.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_escrow_charge_ref"
down_revision = "20260708_escrow_recipient"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("charge_reference", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "charge_reference")
