"""seller Paystack transfer recipient code (escrow payouts)

Revision ID: 20260708_escrow_recipient
Revises: 20260708_storefront_escrow
Create Date: 2026-07-08

Adds ``user.paystack_recipient_code`` — the reusable Paystack Transfer Recipient
(RCP_...) created from the seller's payout/bank details, used to release escrow
funds to the seller.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_escrow_recipient"
down_revision = "20260708_storefront_escrow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("paystack_recipient_code", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "paystack_recipient_code")
