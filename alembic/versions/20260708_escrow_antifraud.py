"""escrow anti-fraud: payout freeze, delivery code, review hold

Revision ID: 20260708_escrow_antifraud
Revises: 20260708_terms_accepted
Create Date: 2026-07-08

Adds:
- ``user.payout_frozen_until`` — escrow payouts paused after a bank/payout change.
- ``storefront_order_escrow.confirmation_code`` — buyer-only delivery code.
- ``storefront_order_escrow.held_for_review`` / ``review_reason`` — collusion/anomaly
  holds that never auto-release (admin decides).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_escrow_antifraud"
down_revision = "20260708_terms_accepted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("payout_frozen_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("confirmation_code", sa.String(length=12), nullable=True),
    )
    op.create_index(
        "ix_storefront_order_escrow_confirmation_code",
        "storefront_order_escrow",
        ["confirmation_code"],
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column(
            "held_for_review",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("review_reason", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "review_reason")
    op.drop_column("storefront_order_escrow", "held_for_review")
    op.drop_index(
        "ix_storefront_order_escrow_confirmation_code",
        table_name="storefront_order_escrow",
    )
    op.drop_column("storefront_order_escrow", "confirmation_code")
    op.drop_column("user", "payout_frozen_until")
