"""add performance indexes

Revision ID: 0033_perf_indexes
Revises: add_pit_amount_20251110
Create Date: 2025-11-21 00:00:00.000000

Adds database indexes for frequently queried columns to improve performance.
Targets invoice queries by user_id, status, and created_at.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0033_perf_indexes"
down_revision = "add_pit_amount_20251110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add performance indexes for invoice queries.

    Indexes added:
    - invoice.issuer_id (already exists via ForeignKey, verify)
    - invoice.status (filter by payment status)
    - invoice.created_at (sort by date, date range queries)
    - invoice.invoice_type (filter revenue vs expense)
    - invoice.category (filter expense categories)

    Composite indexes:
    - (issuer_id, status) - common filter: user's unpaid invoices
    - (issuer_id, created_at) - common query: user's recent invoices
    """
    # Single column indexes (if not already exist)
    op.create_index("ix_invoice_status", "invoice", ["status"], unique=False, if_not_exists=True)

    op.create_index("ix_invoice_created_at", "invoice", ["created_at"], unique=False, if_not_exists=True)

    # Composite indexes for common query patterns
    op.create_index("ix_invoice_issuer_status", "invoice", ["issuer_id", "status"], unique=False, if_not_exists=True)

    op.create_index(
        "ix_invoice_issuer_created", "invoice", ["issuer_id", "created_at"], unique=False, if_not_exists=True
    )

    # Index for expense queries
    op.create_index(
        "ix_invoice_type_category", "invoice", ["invoice_type", "category"], unique=False, if_not_exists=True
    )


def downgrade() -> None:
    """Remove performance indexes."""
    op.drop_index("ix_invoice_type_category", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_issuer_created", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_issuer_status", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_created_at", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_status", table_name="invoice", if_exists=True)
