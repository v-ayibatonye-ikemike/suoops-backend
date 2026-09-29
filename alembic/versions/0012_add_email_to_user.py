"""Add email field to User table for email-based auth

Revision ID: 0012_add_email_to_user
Revises: 0011_enable_whatsapp_otp_auth
Create Date: 2025-10-29 13:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_add_email_to_user"
down_revision = "0011_enable_whatsapp_otp_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add email field to user table for temporary pre-launch email auth."""
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "email",
                sa.String(length=255),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_user_email",
            ["email"],
            unique=True,
        )


def downgrade() -> None:
    """Remove email field from user table."""
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index("ix_user_email")
        batch_op.drop_column("email")
