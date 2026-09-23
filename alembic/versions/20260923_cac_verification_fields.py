"""Add CAC (Corporate Affairs Commission) verification fields to tax_profiles.

Revision ID: 20260923_cac_verification
Revises: 20260922_quick_sale_pm
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260923_cac_verification"
down_revision = "20260922_quick_sale_pm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tax_profiles",
        sa.Column("rc_number", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "tax_profiles",
        sa.Column("cac_verified", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "tax_profiles",
        sa.Column("cac_registered_name", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "tax_profiles",
        sa.Column("cac_verified_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_tax_profiles_rc_number",
        "tax_profiles",
        ["rc_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_tax_profiles_rc_number", table_name="tax_profiles")
    op.drop_column("tax_profiles", "cac_verified_at")
    op.drop_column("tax_profiles", "cac_registered_name")
    op.drop_column("tax_profiles", "cac_verified")
    op.drop_column("tax_profiles", "rc_number")
