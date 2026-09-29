"""card fingerprint on escrow + blocked_card table

Revision ID: 20260710_card_risk
Revises: 20260710_buyer_rep_decay
Create Date: 2026-07-10

Adds ``storefront_order_escrow.card_fingerprint`` (stable per-card id of the
funding card) and the ``blocked_card`` table — used to spot one card funding
many orders and to hold orders paid with a known-bad card for admin review
(card-fraud / chargeback-laundering mitigation).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_card_risk"
down_revision = "20260710_buyer_rep_decay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("card_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_storefront_order_escrow_card_fingerprint",
        "storefront_order_escrow",
        ["card_fingerprint"],
    )
    op.create_table(
        "blocked_card",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("reason", sa.String(length=120), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_blocked_card_fingerprint", "blocked_card", ["fingerprint"], unique=True)
    op.create_index("ix_blocked_card_blocked_until", "blocked_card", ["blocked_until"])


def downgrade() -> None:
    op.drop_index("ix_blocked_card_blocked_until", table_name="blocked_card")
    op.drop_index("ix_blocked_card_fingerprint", table_name="blocked_card")
    op.drop_table("blocked_card")
    op.drop_index("ix_storefront_order_escrow_card_fingerprint", table_name="storefront_order_escrow")
    op.drop_column("storefront_order_escrow", "card_fingerprint")
