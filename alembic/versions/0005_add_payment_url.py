"""add payment_url to invoice

Revision ID: 0005_payment_url
Revises: 0004_tz_aware
Create Date: 2025-10-22 11:15:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_payment_url"
down_revision = "0004_tz_aware"
branch_labels = None
depends_on = None


def upgrade():
    # Add payment_url column to invoice table
    op.add_column("invoice", sa.Column("payment_url", sa.String(), nullable=True))


def downgrade():
    # Remove payment_url column
    op.drop_column("invoice", "payment_url")
