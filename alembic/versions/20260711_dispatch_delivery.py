"""courier name + expected delivery date on storefront escrow dispatch

Revision ID: 20260711_dispatch_delivery
Revises: 20260710_card_risk
Create Date: 2026-07-11

Adds ``dispatch_carrier`` (courier/company name, e.g. "GIG Logistics") and
``dispatch_eta`` (seller's expected delivery date) to ``storefront_order_escrow``.
Both are shown to the buyer at "mark as sent out" so they know who's bringing the
order and roughly when to expect it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260711_dispatch_delivery"
down_revision = "20260710_card_risk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("dispatch_carrier", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("dispatch_eta", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "dispatch_eta")
    op.drop_column("storefront_order_escrow", "dispatch_carrier")
