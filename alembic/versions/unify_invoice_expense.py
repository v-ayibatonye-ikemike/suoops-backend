"""Unify Invoice and Expense models

Revision ID: unify_invoice_expense
Revises: add_expense_tracking
Create Date: 2025-11-10 00:00:00.000000

This migration:
1. Adds invoice_type, category, vendor_name, receipt_url, receipt_text to invoice table
2. Migrates existing expense records to invoice table
3. Keeps expenses table for backward compatibility (will be deprecated)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "unify_invoice_expense"
down_revision: Union[str, None] = "add_expense_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add expense fields to invoice table and migrate expense data"""

    # Add new columns to invoice table
    op.add_column("invoice", sa.Column("invoice_type", sa.String(20), nullable=True))
    op.add_column("invoice", sa.Column("category", sa.String(50), nullable=True))
    op.add_column("invoice", sa.Column("vendor_name", sa.String(200), nullable=True))
    op.add_column("invoice", sa.Column("receipt_url", sa.String(500), nullable=True))
    op.add_column("invoice", sa.Column("receipt_text", sa.Text(), nullable=True))
    op.add_column("invoice", sa.Column("input_method", sa.String(20), nullable=True))
    op.add_column("invoice", sa.Column("channel", sa.String(20), nullable=True))
    op.add_column("invoice", sa.Column("merchant", sa.String(200), nullable=True))
    op.add_column("invoice", sa.Column("verified", sa.Boolean(), default=False, nullable=True))
    op.add_column("invoice", sa.Column("notes", sa.Text(), nullable=True))

    # Set default invoice_type for existing invoices (they are all revenue)
    op.execute("UPDATE invoice SET invoice_type = 'revenue' WHERE invoice_type IS NULL")

    # Now make invoice_type non-nullable with default
    op.alter_column("invoice", "invoice_type", nullable=False, server_default="revenue")

    # Create indexes for efficient filtering
    op.create_index("ix_invoice_type", "invoice", ["invoice_type"])
    op.create_index("ix_invoice_category", "invoice", ["category"])

    # Migrate expense records to invoice table
    # Get connection for data migration
    connection = op.get_bind()

    # Check if expenses table exists and has data
    result = connection.execute(
        text("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'expenses'
        )
    """)
    )

    expenses_table_exists = result.scalar()

    if expenses_table_exists:
        # Count expenses to migrate
        count_result = connection.execute(text("SELECT COUNT(*) FROM expenses"))
        expense_count = count_result.scalar()

        if expense_count > 0:
            # Migrate expense data to invoice table
            # Note: We need to create customer records for vendors first
            connection.execute(
                text("""
                -- Create customer records for each unique vendor (for expense invoices)
                INSERT INTO customer (name, phone, created_at)
                SELECT DISTINCT 
                    COALESCE(merchant, 'Unknown Vendor') as name,
                    NULL as phone,
                    NOW() as created_at
                FROM expenses
                WHERE merchant IS NOT NULL 
                AND merchant NOT IN (SELECT name FROM customer)
                ON CONFLICT DO NOTHING;
                
                -- Also create a default 'Expense Vendor' customer for expenses without merchant
                INSERT INTO customer (name, phone, created_at)
                VALUES ('Expense Vendor', NULL, NOW())
                ON CONFLICT DO NOTHING;
            """)
            )

            # Migrate expenses to invoices
            connection.execute(
                text("""
                INSERT INTO invoice (
                    invoice_id, 
                    issuer_id, 
                    customer_id, 
                    amount, 
                    status, 
                    created_at, 
                    paid_at,
                    invoice_type, 
                    category, 
                    vendor_name, 
                    merchant,
                    receipt_url, 
                    receipt_text, 
                    input_method, 
                    channel, 
                    verified, 
                    notes,
                    due_date,
                    vat_rate,
                    vat_amount
                )
                SELECT 
                    'EXP-' || e.id || '-' || EXTRACT(EPOCH FROM e.created_at)::bigint as invoice_id,
                    e.user_id as issuer_id,
                    COALESCE(
                        (SELECT id FROM customer WHERE name = COALESCE(e.merchant, 'Expense Vendor') LIMIT 1),
                        (SELECT id FROM customer WHERE name = 'Expense Vendor' LIMIT 1)
                    ) as customer_id,
                    e.amount,
                    CASE WHEN e.verified THEN 'paid' ELSE 'pending' END as status,
                    e.created_at,
                    CASE WHEN e.verified THEN e.created_at ELSE NULL END as paid_at,
                    'expense' as invoice_type,
                    e.category,
                    COALESCE(e.merchant, 'Unknown Vendor') as vendor_name,
                    e.merchant,
                    e.receipt_url,
                    e.receipt_text,
                    e.input_method,
                    e.channel,
                    e.verified,
                    e.notes,
                    e.date as due_date,
                    0 as vat_rate,
                    0 as vat_amount
                FROM expenses e
                ON CONFLICT (invoice_id) DO NOTHING;
            """)
            )


def downgrade() -> None:
    """Remove expense fields from invoice table"""

    # Remove indexes
    op.drop_index("ix_invoice_category", table_name="invoice")
    op.drop_index("ix_invoice_type", table_name="invoice")

    # Remove columns
    op.drop_column("invoice", "notes")
    op.drop_column("invoice", "verified")
    op.drop_column("invoice", "merchant")
    op.drop_column("invoice", "channel")
    op.drop_column("invoice", "input_method")
    op.drop_column("invoice", "receipt_text")
    op.drop_column("invoice", "receipt_url")
    op.drop_column("invoice", "vendor_name")
    op.drop_column("invoice", "category")
    op.drop_column("invoice", "invoice_type")
