"""add storefront_description to user

Revision ID: 20260706_storefront_description
Revises: 20260706_wallet_starter_default
Create Date: 2026-07-05

Short public description of what a shop sells, shown in the storefront
directory so customers know what they're clicking into.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260706_storefront_description"
down_revision = "20260706_wallet_starter_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("storefront_description", sa.String(length=160), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "storefront_description")
