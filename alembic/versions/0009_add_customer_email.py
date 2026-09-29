"""add customer email

Revision ID: 0009_add_customer_email
Revises: 0008_add_logo_url
Create Date: 2025-10-22

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_add_customer_email"
down_revision = "0008_add_logo_url"
branch_labels = None
depends_on = None


def upgrade():
    """Add email field to customer table for invoice sending."""
    op.add_column("customer", sa.Column("email", sa.String(255), nullable=True))


def downgrade():
    """Remove email field."""
    op.drop_column("customer", "email")
