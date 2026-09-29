"""seller delivery proof + buyer reputation

Revision ID: 20260708_delivery_buyer_rep
Revises: 20260708_escrow_antifraud
Create Date: 2026-07-08

Adds:
- ``storefront_order_escrow`` seller delivery-proof columns (mark-delivered
  timestamp, note, proof image URL) — defends against false "not delivered" claims.
- ``buyer_reputation`` table — global, phone-keyed dispute / false-dispute counts.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_delivery_buyer_rep"
down_revision = "20260708_escrow_antifraud"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("seller_marked_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_proof_note", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_proof_url", sa.String(length=500), nullable=True),
    )

    op.create_table(
        "buyer_reputation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("disputes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("false_disputes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("flagged", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_buyer_reputation_phone", "buyer_reputation", ["phone"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_buyer_reputation_phone", table_name="buyer_reputation")
    op.drop_table("buyer_reputation")
    op.drop_column("storefront_order_escrow", "delivery_proof_url")
    op.drop_column("storefront_order_escrow", "delivery_proof_note")
    op.drop_column("storefront_order_escrow", "seller_marked_delivered_at")
