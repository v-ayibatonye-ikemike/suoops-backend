"""Add pro_override column to users table

Admin-granted PRO feature access without subscription or invoice packs.
When True, user gets PRO features but keeps their actual plan and invoice balance.

Revision ID: user_pro_override_20260220
Revises: tax_profile_fields_20260220
Create Date: 2026-02-20
"""

import sqlalchemy as sa

from alembic import op

revision = "user_pro_override_20260220"
down_revision = "tax_profile_fields_20260220"


def upgrade():
    op.add_column(
        "user",
        sa.Column("pro_override", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade():
    op.drop_column("user", "pro_override")
