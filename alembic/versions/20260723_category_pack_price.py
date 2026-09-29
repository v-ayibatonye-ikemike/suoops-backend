"""Add product_category.pack_price

Revision ID: 20260723_category_pack_price
Revises: 20260723_team_max_members_5
Create Date: 2026-07-23

Lets a seller attach a packaging/pack fee to a product category (e.g. takeaway
packs for cooked food). At storefront checkout, a single flat pack fee is added
automatically to any order that contains a product in a packaged category.

NOTE: revision id kept <=32 chars — alembic_version.version_num is varchar(32).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260723_category_pack_price"
down_revision = "20260723_team_max_members_5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_category",
        sa.Column("pack_price", sa.Numeric(15, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_category", "pack_price")
