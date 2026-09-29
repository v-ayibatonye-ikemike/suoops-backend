"""Add created_by_user_id to invoice table.

Revision ID: 20251210_add_invoice_created_by
Revises: add_expense_tracking
Create Date: 2024-12-10

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251210_add_invoice_created_by"
down_revision = "20251209_sub_started"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add created_by_user_id column to track who created the invoice
    op.add_column("invoice", sa.Column("created_by_user_id", sa.Integer(), nullable=True))

    # Add index for performance
    op.create_index(op.f("ix_invoice_created_by_user_id"), "invoice", ["created_by_user_id"], unique=False)

    # Add foreign key constraint
    op.create_foreign_key("fk_invoice_created_by_user_id", "invoice", "user", ["created_by_user_id"], ["id"])

    # Backfill existing invoices: set created_by_user_id = issuer_id for all existing invoices
    # This ensures existing invoices can still be confirmed by the issuer
    op.execute("UPDATE invoice SET created_by_user_id = issuer_id WHERE created_by_user_id IS NULL")


def downgrade() -> None:
    op.drop_constraint("fk_invoice_created_by_user_id", "invoice", type_="foreignkey")
    op.drop_index(op.f("ix_invoice_created_by_user_id"), table_name="invoice")
    op.drop_column("invoice", "created_by_user_id")
