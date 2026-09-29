"""make user phone nullable

Revision ID: 20251229_phone_nullable
Revises: 20251224_remove_business_plan
Create Date: 2025-12-29 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251229_phone_nullable"
down_revision = "20251224_remove_business"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make phone column nullable to allow:
    # 1. Email-only signups (no phone initially)
    # 2. Users to remove/change their WhatsApp number
    op.alter_column("user", "phone", existing_type=sa.VARCHAR(length=32), nullable=True)


def downgrade() -> None:
    # Note: This will fail if there are NULL phone values in the database
    op.alter_column("user", "phone", existing_type=sa.VARCHAR(length=32), nullable=False)
