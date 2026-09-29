"""add user role column

Revision ID: 20251110_03_add_role
Revises: 20251110_02_merge
Create Date: 2025-11-10 10:45:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251110_03_add_role"
down_revision = "20251110_02_merge"
branch_labels = None
depends_on = None


def upgrade():
    # Add role column with default value 'user'
    op.add_column("user", sa.Column("role", sa.String(length=20), server_default="user", nullable=False))
    op.create_index(op.f("ix_user_role"), "user", ["role"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_user_role"), table_name="user")
    op.drop_column("user", "role")
