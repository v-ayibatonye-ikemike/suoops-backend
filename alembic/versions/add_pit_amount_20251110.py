"""add pit_amount to monthly_tax_reports

Revision ID: add_pit_amount_20251110
Revises: unify_invoice_expense
Create Date: 2025-11-10 16:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "add_pit_amount_20251110"
down_revision = "unify_invoice_expense"
branch_labels = None
depends_on = None


def upgrade():
    # Add pit_amount column to monthly_tax_reports table
    op.add_column(
        "monthly_tax_reports",
        sa.Column("pit_amount", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
    )


def downgrade():
    # Remove pit_amount column
    op.drop_column("monthly_tax_reports", "pit_amount")
