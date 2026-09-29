"""Delivery service type + dropoff station on storefront escrow

Revision ID: 20260711_delivery_service_type
Revises: 20260711_shipbubble_booking
Create Date: 2026-07-11

Adds ``delivery_service_type`` ("pickup"/"dropoff") and ``delivery_dropoff_station``
to ``storefront_order_escrow`` so the seller can be told whether a rider will
collect from them or they must drop the package at a courier station.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260711_delivery_service_type"
down_revision = "20260711_shipbubble_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_service_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("delivery_dropoff_station", sa.String(length=300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "delivery_dropoff_station")
    op.drop_column("storefront_order_escrow", "delivery_service_type")
