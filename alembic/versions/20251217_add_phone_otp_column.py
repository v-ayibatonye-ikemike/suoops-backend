"""Add phone_otp column to user table for WhatsApp verification.

Revision ID: 20251217_phone_otp
Revises: 20251210_status_updated_by
Create Date: 2025-12-17
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251217_phone_otp"
down_revision = "20251210_status_updated_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add phone_otp column for temporary OTP storage during verification
    op.add_column(
        "user",
        sa.Column("phone_otp", sa.String(6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "phone_otp")
