"""Increase customer_phone column length to 32 for OAuth synthetic phones.

Revision ID: fix_customer_phone_len
Revises: 20251217_phone_otp
Create Date: 2025-12-17

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "fix_customer_phone_len"
down_revision = "20251217_phone_otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Increase customer_phone column length from VARCHAR(20) to VARCHAR(32)
    # This is needed to support OAuth synthetic phone numbers like:
    # "oauth_google_ikemikeayibatonye" (30 characters)
    op.alter_column(
        "payment_transactions",
        "customer_phone",
        existing_type=sa.String(20),
        type_=sa.String(32),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "payment_transactions",
        "customer_phone",
        existing_type=sa.String(32),
        type_=sa.String(20),
        existing_nullable=True,
    )
