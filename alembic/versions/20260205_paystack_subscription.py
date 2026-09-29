"""Add Paystack subscription fields to User model.

Supports auto-recurring Paystack subscriptions for Pro/Business plans.

Revision ID: 20260205_paystack_subscription
Revises: invoice_user_set_null
Create Date: 2026-02-05
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260205_paystack_subscription"
down_revision = "invoice_user_set_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add Paystack subscription tracking fields
    op.add_column("user", sa.Column("paystack_subscription_code", sa.String(100), nullable=True))
    op.add_column("user", sa.Column("paystack_customer_code", sa.String(100), nullable=True))

    # Index for efficient subscription lookup
    op.create_index("ix_user_paystack_subscription_code", "user", ["paystack_subscription_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_paystack_subscription_code", table_name="user")
    op.drop_column("user", "paystack_customer_code")
    op.drop_column("user", "paystack_subscription_code")
