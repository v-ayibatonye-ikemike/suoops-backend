"""Add subscription_started_at column to user table

Revision ID: 20251209_sub_started
Revises: 20251209_sub_expiry
Create Date: 2025-12-09

This migration adds the subscription_started_at column that was missing
from the initial subscription_expiry migration.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251209_sub_started"
down_revision = "20251209_sub_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add subscription_started_at column if it doesn't exist
    # Use raw SQL to check if column exists first
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'subscription_started_at'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "user",
            sa.Column(
                "subscription_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="When the current paid subscription started. For billing cycle calculations.",
            ),
        )


def downgrade() -> None:
    op.drop_column("user", "subscription_started_at")
