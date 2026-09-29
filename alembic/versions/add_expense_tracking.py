"""Add expense tracking table

Revision ID: add_expense_tracking
Revises: add_period_type_tax
Create Date: 2025-11-10

This migration adds the expenses table to support multi-channel expense tracking
(WhatsApp, email, dashboard) for accurate profit calculation and 2026 tax compliance.

Profit = Revenue - Expenses (per 2026 Nigerian Tax Law)
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "add_expense_tracking"
down_revision = "add_period_type_tax"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy import inspect

    # Get database connection
    conn = op.get_bind()
    inspector = inspect(conn)

    # Check if table exists
    if "expenses" not in inspector.get_table_names():
        # Create expenses table
        op.create_table(
            "expenses",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
            # Amount & Date
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            # Categorization
            sa.Column("category", sa.String(50), nullable=False),
            sa.Column("description", sa.String(500), nullable=True),
            sa.Column("merchant", sa.String(200), nullable=True),
            # Source tracking
            sa.Column("input_method", sa.String(20), nullable=True),
            sa.Column("channel", sa.String(20), nullable=True),
            # Receipt/Evidence
            sa.Column("receipt_url", sa.String(500), nullable=True),
            sa.Column("receipt_text", sa.Text(), nullable=True),
            # Verification
            sa.Column("verified", sa.Boolean(), default=False, server_default="false"),
            sa.Column("notes", sa.Text(), nullable=True),
            # Metadata
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Get existing indexes
    existing_indexes = (
        [idx["name"] for idx in inspector.get_indexes("expenses")] if "expenses" in inspector.get_table_names() else []
    )

    # Create indexes only if they don't exist
    if "ix_expenses_user_id" not in existing_indexes:
        op.create_index("ix_expenses_user_id", "expenses", ["user_id"])

    if "ix_expenses_date" not in existing_indexes:
        op.create_index("ix_expenses_date", "expenses", ["date"])

    if "ix_expenses_category" not in existing_indexes:
        op.create_index("ix_expenses_category", "expenses", ["category"])

    if "ix_expenses_user_date" not in existing_indexes:
        op.create_index("ix_expenses_user_date", "expenses", ["user_id", "date"])

    # Get existing constraints
    existing_constraints = (
        [const["name"] for const in inspector.get_check_constraints("expenses")]
        if "expenses" in inspector.get_table_names()
        else []
    )

    # Add check constraints only if they don't exist
    if "ck_expense_category" not in existing_constraints:
        op.create_check_constraint(
            "ck_expense_category",
            "expenses",
            """category IN (
                'rent', 'utilities', 'data_internet', 'transport', 'supplies',
                'equipment', 'marketing', 'professional_fees', 'staff_wages',
                'maintenance', 'other'
            )""",
        )

    if "ck_expense_input_method" not in existing_constraints:
        op.create_check_constraint(
            "ck_expense_input_method",
            "expenses",
            "input_method IN ('voice', 'text', 'photo', 'manual') OR input_method IS NULL",
        )

    if "ck_expense_channel" not in existing_constraints:
        op.create_check_constraint(
            "ck_expense_channel", "expenses", "channel IN ('whatsapp', 'email', 'dashboard') OR channel IS NULL"
        )

    if "ck_expense_amount_positive" not in existing_constraints:
        op.create_check_constraint("ck_expense_amount_positive", "expenses", "amount > 0")


def downgrade():
    op.drop_table("expenses")
