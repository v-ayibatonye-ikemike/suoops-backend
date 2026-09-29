"""Add subscription_expires_at to user table

Revision ID: 20251209_sub_expiry
Revises: 20251209_add_teams
Create Date: 2025-12-09

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251209_sub_expiry"
down_revision = "20251209_add_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add subscription_expires_at column to track when paid plan expires
    op.add_column(
        "user",
        sa.Column(
            "subscription_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the current paid subscription expires. NULL for free tier.",
        ),
    )
    # Add subscription_started_at column to track when paid plan started
    op.add_column(
        "user",
        sa.Column(
            "subscription_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the current paid subscription started. For billing cycle calculations.",
        ),
    )
    # Add index for efficient expiry checks
    op.create_index("ix_user_subscription_expires_at", "user", ["subscription_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_subscription_expires_at", table_name="user")
    op.drop_column("user", "subscription_started_at")
    op.drop_column("user", "subscription_expires_at")
