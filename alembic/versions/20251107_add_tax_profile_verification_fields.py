"""add tax profile verification fields

Revision ID: 20251107_tax_profile_verify
Revises: 20251107_add_alert_events
Create Date: 2025-11-07 (revised to shorten revision ID < 32 chars)
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251107_tax_profile_verify"
down_revision = "20251107_add_alert_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add columns to tax_profiles table if they do not already exist
    table = "tax_profiles"
    if _table_exists(table):
        existing = _get_columns(table)
        if "tin_verified" not in existing:
            op.add_column(table, sa.Column("tin_verified", sa.Boolean(), server_default=sa.text("false")))
        if "vat_verified" not in existing:
            op.add_column(table, sa.Column("vat_verified", sa.Boolean(), server_default=sa.text("false")))
        if "verification_status" not in existing:
            op.add_column(table, sa.Column("verification_status", sa.String(20), server_default="pending"))
        if "verification_attempts" not in existing:
            op.add_column(table, sa.Column("verification_attempts", sa.Integer(), server_default="0"))
        if "last_verification_at" not in existing:
            op.add_column(table, sa.Column("last_verification_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    table = "tax_profiles"
    if _table_exists(table):
        existing = _get_columns(table)
        for col in [
            "last_verification_at",
            "verification_attempts",
            "verification_status",
            "vat_verified",
            "tin_verified",
        ]:
            if col in existing:
                op.drop_column(table, col)


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _get_columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns(table_name)}
