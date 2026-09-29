"""Add storefront_order_escrow.requires_delivery

Revision ID: 20260723_escrow_reqdelivery
Revises: 20260723_product_fulfilment_type
Create Date: 2026-07-23

Service/digital storefront orders aren't shipped, so their buyer protection is
confirmation + fast auto-release rather than a delivery cycle. This flag lets
the seller notification, dashboard panel and copy drop delivery/dispatch steps
for them. Existing orders default to ``true`` (physical) so nothing changes.

NOTE: revision id kept <=32 chars — alembic_version.version_num is varchar(32).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260723_escrow_reqdelivery"
down_revision = "20260723_product_fulfilment_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column(
            "requires_delivery",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "requires_delivery")
