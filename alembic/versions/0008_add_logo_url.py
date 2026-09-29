"""add logo url

Revision ID: 0008_add_logo_url
Revises: 0007_fix_enum_case
Create Date: 2025-10-22

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_add_logo_url"
down_revision = "0007_fix_enum_case"
branch_labels = None
depends_on = None


def upgrade():
    """Add logo_url field to user table for business logo."""
    op.add_column("user", sa.Column("logo_url", sa.String(512), nullable=True))


def downgrade():
    """Remove logo_url field."""
    op.drop_column("user", "logo_url")
