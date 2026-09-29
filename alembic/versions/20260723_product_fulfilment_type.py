"""Add product.fulfilment_type (physical | service | digital)

Revision ID: 20260723_product_fulfilment_type
Revises: 20260721_invoice_fee_ledger
Create Date: 2026-07-23

Distinguishes physical goods (which need delivery) from services and digital
products (which do not). Service/digital storefront orders skip the delivery
address + courier and use a faster buyer-protection window. Existing rows
default to ``physical`` so today's behaviour is unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260723_product_fulfilment_type"
down_revision = "20260721_invoice_fee_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product",
        sa.Column(
            "fulfilment_type",
            sa.String(length=20),
            nullable=False,
            server_default="physical",
        ),
    )
    # Historically a product with track_stock=false was created via the form's
    # "Service" toggle (freelance/digital — no stock), so treat those as services
    # up front. Stocked products stay physical.
    op.execute("UPDATE product SET fulfilment_type = 'service' WHERE track_stock = false")


def downgrade() -> None:
    op.drop_column("product", "fulfilment_type")
