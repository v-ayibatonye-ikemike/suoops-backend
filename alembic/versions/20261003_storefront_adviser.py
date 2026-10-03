"""Add merchant-controlled storefront merchandising fields.

Revision ID: 20261003_storefront_ai
Revises: 20261003_collections
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261003_storefront_ai"
down_revision = "20261003_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product",
        sa.Column("storefront_featured", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "product",
        sa.Column("storefront_discount_percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "product",
        sa.Column("storefront_bundle_label", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product", "storefront_bundle_label")
    op.drop_column("product", "storefront_discount_percent")
    op.drop_column("product", "storefront_featured")
