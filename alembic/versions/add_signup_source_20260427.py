"""add signup_source column to user table

Revision ID: 20260427_signup_source
Revises: 20260406_ix_invoice_created_at
Create Date: 2026-04-27
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "20260427_signup_source"
down_revision = "20260406_ix_invoice_created_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("signup_source", sa.String(50), nullable=True))
    op.create_index("ix_user_signup_source", "user", ["signup_source"])


def downgrade() -> None:
    op.drop_index("ix_user_signup_source", table_name="user")
    op.drop_column("user", "signup_source")
