"""storefront order escrow (buyer protection) + business GPS location

Revision ID: 20260708_storefront_escrow
Revises: 20260708_store_moderation_fraud
Create Date: 2026-07-08

Adds:
  - ``user.storefront_lat`` / ``user.storefront_lng`` : GPS-captured business
    location (powers the escrow same/different-state window + future delivery).
  - ``storefront_order_escrow`` table : per-order buyer-protection hold. The
    customer's payment is held; the seller is paid out (Paystack Transfer, minus
    commission) on buyer confirmation or window expiry; refunded on valid dispute.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_storefront_escrow"
down_revision = "20260708_store_moderation_fraud"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Business GPS location ──
    op.add_column("user", sa.Column("storefront_lat", sa.Float(), nullable=True))
    op.add_column("user", sa.Column("storefront_lng", sa.Float(), nullable=True))

    # ── Per-order escrow hold ──
    op.create_table(
        "storefront_order_escrow",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "invoice_id",
            sa.Integer(),
            sa.ForeignKey("invoice.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seller_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="held", nullable=False),
        sa.Column("same_state", sa.Boolean(), nullable=True),
        sa.Column("release_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gross_kobo", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fee_kobo", sa.Integer(), server_default="0", nullable=False),
        sa.Column("payout_kobo", sa.Integer(), server_default="0", nullable=False),
        sa.Column("business_state", sa.String(length=80), nullable=True),
        sa.Column("customer_state", sa.String(length=80), nullable=True),
        sa.Column("customer_lat", sa.Float(), nullable=True),
        sa.Column("customer_lng", sa.Float(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispute_reason", sa.String(length=255), nullable=True),
        sa.Column("transfer_recipient_code", sa.String(length=100), nullable=True),
        sa.Column("transfer_reference", sa.String(length=100), nullable=True),
        sa.Column("refund_reference", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_storefront_order_escrow_invoice_id",
        "storefront_order_escrow",
        ["invoice_id"],
        unique=True,
    )
    op.create_index("ix_storefront_order_escrow_seller_id", "storefront_order_escrow", ["seller_id"])
    op.create_index("ix_storefront_order_escrow_status", "storefront_order_escrow", ["status"])
    op.create_index("ix_storefront_order_escrow_release_due_at", "storefront_order_escrow", ["release_due_at"])
    op.create_index("ix_storefront_order_escrow_transfer_reference", "storefront_order_escrow", ["transfer_reference"])


def downgrade() -> None:
    op.drop_index("ix_storefront_order_escrow_transfer_reference", table_name="storefront_order_escrow")
    op.drop_index("ix_storefront_order_escrow_release_due_at", table_name="storefront_order_escrow")
    op.drop_index("ix_storefront_order_escrow_status", table_name="storefront_order_escrow")
    op.drop_index("ix_storefront_order_escrow_seller_id", table_name="storefront_order_escrow")
    op.drop_index("ix_storefront_order_escrow_invoice_id", table_name="storefront_order_escrow")
    op.drop_table("storefront_order_escrow")
    op.drop_column("user", "storefront_lng")
    op.drop_column("user", "storefront_lat")
