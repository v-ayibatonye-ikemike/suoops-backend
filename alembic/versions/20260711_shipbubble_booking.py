"""Shipbubble courier booking fields on storefront escrow

Revision ID: 20260711_shipbubble_booking
Revises: 20260711_dispatch_delivery
Create Date: 2026-07-11

Adds delivery-fee + Shipbubble booking fields to ``storefront_order_escrow`` so a
buyer-paid delivery can be captured at checkout and the courier booked at
dispatch. The delivery fee is retained by SuoOps to fund the courier (never part
of the seller payout). Feature-flagged via SHIPBUBBLE_CHECKOUT_ENABLED.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260711_shipbubble_booking"
down_revision = "20260711_dispatch_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_fee_kobo", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_courier", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_request_token", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_courier_id", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_service_code", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("shipbubble_order_id", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("shipbubble_tracking_url", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_storefront_order_escrow_shipbubble_order_id",
        "storefront_order_escrow",
        ["shipbubble_order_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_storefront_order_escrow_shipbubble_order_id",
        table_name="storefront_order_escrow",
    )
    for col in (
        "shipbubble_tracking_url",
        "shipbubble_order_id",
        "delivery_service_code",
        "delivery_courier_id",
        "delivery_request_token",
        "delivery_courier",
        "delivery_fee_kobo",
    ):
        op.drop_column("storefront_order_escrow", col)
