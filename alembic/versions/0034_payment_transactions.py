"""add payment_transactions table

Revision ID: 0034_payment_transactions
Revises: 0033_perf_indexes
Create Date: 2025-11-21 12:00:00.000000

Adds payment_transactions table for tracking subscription payments via Paystack.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0034_payment_transactions"
down_revision = "0033_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create payment_transactions table."""
    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("plan_before", sa.String(length=20), nullable=False),
        sa.Column("plan_after", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SUCCESS", "FAILED", "CANCELLED", "REFUNDED", name="paymentstatus"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum("PAYSTACK", "STRIPE", "MANUAL", name="paymentprovider"),
            nullable=False,
            server_default="PAYSTACK",
        ),
        sa.Column("paystack_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("paystack_authorization_url", sa.Text(), nullable=True),
        sa.Column("paystack_access_code", sa.String(length=100), nullable=True),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("card_last4", sa.String(length=4), nullable=True),
        sa.Column("card_brand", sa.String(length=20), nullable=True),
        sa.Column("bank_name", sa.String(length=100), nullable=True),
        sa.Column("customer_email", sa.String(length=255), nullable=False),
        sa.Column("customer_phone", sa.String(length=20), nullable=True),
        sa.Column("billing_start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("payment_metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Add foreign key constraint only if users table exists
    # In production, users table exists but not in migration history
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "users" in inspector.get_table_names():
        op.create_foreign_key("fk_payment_transactions_user_id", "payment_transactions", "users", ["user_id"], ["id"])

    # Indexes for performance
    op.create_index("ix_payment_transactions_user_id", "payment_transactions", ["user_id"])
    op.create_index("ix_payment_transactions_reference", "payment_transactions", ["reference"], unique=True)
    op.create_index("ix_payment_transactions_status", "payment_transactions", ["status"])
    op.create_index("ix_payment_transactions_created_at", "payment_transactions", ["created_at"])
    op.create_index("ix_payment_transactions_paid_at", "payment_transactions", ["paid_at"])


def downgrade() -> None:
    """Drop payment_transactions table."""
    op.drop_index("ix_payment_transactions_paid_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_created_at", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_status", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_reference", table_name="payment_transactions")
    op.drop_index("ix_payment_transactions_user_id", table_name="payment_transactions")
    op.drop_table("payment_transactions")
    op.execute("DROP TYPE paymentstatus")
    op.execute("DROP TYPE paymentprovider")
