"""track payout rail on storefront escrow transfers

Revision ID: 20260710_transfer_provider
Revises: 20260710_seller_dispatch
Create Date: 2026-07-10

Adds ``storefront_order_escrow.transfer_provider`` — the payout rail a transfer
reference was sent on. A reference is only valid on its own provider, so if the
payout rail changes (e.g. an early Paystack attempt that failed on balance, then
routed to Flutterwave), the old reference is void and a fresh transfer is sent
rather than reconciled against the wrong provider (which would deadlock on an
'unknown' status).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_transfer_provider"
down_revision = "20260710_seller_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("transfer_provider", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "transfer_provider")
