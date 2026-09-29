"""fix enum case

Revision ID: 0007_fix_enum_case
Revises: 0006_bank_and_subscriptions
Create Date: 2025-10-22

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_fix_enum_case"
down_revision = "0006_bank_and_subscriptions"
branch_labels = None
depends_on = None


def upgrade():
    """Fix subscription plan enum to use uppercase values."""
    # First, drop the constraint and column
    op.drop_column("user", "plan")

    # Drop the old enum type
    op.execute("DROP TYPE IF EXISTS subscriptionplan")

    # Create new enum type with uppercase values
    subscription_plan = postgresql.ENUM(
        "FREE", "STARTER", "PRO", "BUSINESS", "ENTERPRISE", name="subscriptionplan", create_type=True
    )
    subscription_plan.create(op.get_bind(), checkfirst=True)

    # Re-add the column with the corrected enum
    op.add_column("user", sa.Column("plan", subscription_plan, server_default="FREE", nullable=False))


def downgrade():
    """Revert to lowercase enum values."""
    op.drop_column("user", "plan")
    op.execute("DROP TYPE IF EXISTS subscriptionplan")

    subscription_plan = postgresql.ENUM(
        "free", "starter", "pro", "business", "enterprise", name="subscriptionplan", create_type=True
    )
    subscription_plan.create(op.get_bind(), checkfirst=True)
    op.add_column("user", sa.Column("plan", subscription_plan, server_default="free", nullable=False))
