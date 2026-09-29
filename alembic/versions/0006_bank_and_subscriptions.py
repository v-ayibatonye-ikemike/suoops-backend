"""add subscription and bank details

Revision ID: 0006_bank_and_subscriptions
Revises: 0005_payment_url
Create Date: 2025-10-22

Combines subscription plan tracking and bank account details.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_bank_and_subscriptions"
down_revision = "0005_payment_url"
branch_labels = None
depends_on = None


def upgrade():
    """Add subscription tracking and bank account details.

    Subscription Plans:
    - FREE: 5 invoices/month
    - STARTER: 100 invoices/month
    - PRO: 1,000 invoices/month
    - BUSINESS: 3,000 invoices/month
    - ENTERPRISE: unlimited

    Bank Transfer Model:
    - Businesses configure their bank account details
    - Invoices show bank transfer instructions
    - No payment platform integration
    - Manual payment confirmation
    """
    # Add subscription plan enum type
    subscription_plan = postgresql.ENUM(
        "FREE", "STARTER", "PRO", "BUSINESS", "ENTERPRISE", name="subscriptionplan", create_type=True
    )
    subscription_plan.create(op.get_bind(), checkfirst=True)

    # Add subscription tracking columns
    op.add_column("user", sa.Column("plan", subscription_plan, server_default="FREE", nullable=False))
    op.add_column("user", sa.Column("invoices_this_month", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "user", sa.Column("usage_reset_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )

    # Add bank account details columns
    op.add_column("user", sa.Column("business_name", sa.String(255), nullable=True))
    op.add_column("user", sa.Column("bank_name", sa.String(100), nullable=True))
    op.add_column("user", sa.Column("account_number", sa.String(20), nullable=True))
    op.add_column("user", sa.Column("account_name", sa.String(255), nullable=True))

    # Remove payment platform columns (if they exist)
    op.execute("ALTER TABLE invoice DROP COLUMN IF EXISTS payment_ref")

    # Drop WebhookEvent table if it exists (no longer needed without payment platforms)
    op.execute("DROP TABLE IF EXISTS webhookevent CASCADE")


def downgrade():
    """Revert to previous state."""
    # Recreate webhookevent table
    op.create_table(
        "webhookevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("signature", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_webhookevent_provider"), "webhookevent", ["provider"])

    # Add back payment columns
    op.add_column("invoice", sa.Column("payment_ref", sa.String(), nullable=True))

    # Drop bank account columns
    op.drop_column("user", "account_name")
    op.drop_column("user", "account_number")
    op.drop_column("user", "bank_name")
    op.drop_column("user", "business_name")

    # Drop subscription columns
    op.drop_column("user", "usage_reset_at")
    op.drop_column("user", "invoices_this_month")
    op.drop_column("user", "plan")

    # Drop enum type
    subscription_plan = postgresql.ENUM("FREE", "STARTER", "PRO", "BUSINESS", "ENTERPRISE", name="subscriptionplan")
    subscription_plan.drop(op.get_bind(), checkfirst=True)
