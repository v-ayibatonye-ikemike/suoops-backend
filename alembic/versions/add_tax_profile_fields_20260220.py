"""Add business_type, vat_apply_to, withholding_vat_applies to tax_profiles

Revision ID: tax_profile_fields_20260220
Revises: 20260219_add_missing_indexes
Create Date: 2026-02-20
"""

import sqlalchemy as sa

from alembic import op

revision = "tax_profile_fields_20260220"
down_revision = "20260219_add_missing_indexes"


def upgrade():
    # Add new columns with defaults (safe for existing rows)
    op.add_column(
        "tax_profiles",
        sa.Column("business_type", sa.String(20), server_default="mixed", nullable=True),
    )
    op.add_column(
        "tax_profiles",
        sa.Column("vat_apply_to", sa.String(20), server_default="all", nullable=True),
    )
    op.add_column(
        "tax_profiles",
        sa.Column("withholding_vat_applies", sa.Boolean(), server_default="false", nullable=True),
    )


def downgrade():
    op.drop_column("tax_profiles", "withholding_vat_applies")
    op.drop_column("tax_profiles", "vat_apply_to")
    op.drop_column("tax_profiles", "business_type")
