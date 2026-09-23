"""Add payment_method column for quick-sale (walk-in) invoices.

Revision ID: 20260922_quick_sale_pm
Revises: 20260816_storefront_cover
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260922_quick_sale_pm"
down_revision = "20260816_storefront_cover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice",
        sa.Column("payment_method", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoice", "payment_method")
