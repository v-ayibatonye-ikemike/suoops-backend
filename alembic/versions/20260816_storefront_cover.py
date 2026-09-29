"""Add a landscape storefront cover image.

Revision ID: 20260816_storefront_cover
Revises: 20260728_expense_integrity
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260816_storefront_cover"
down_revision = "20260728_expense_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("storefront_cover_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "storefront_cover_url")
