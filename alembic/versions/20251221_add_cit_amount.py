"""Add cit_amount column to monthly_tax_reports

Revision ID: 20251221_add_cit
Revises: 20251219_add_invoice_balance
Create Date: 2024-12-21

Company Income Tax (CIT) calculation for PRO+ plans:
- Small companies (turnover ≤₦50M): 0% exempt
- Other companies (turnover >₦50M): 30%
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251221_add_cit"
down_revision = "20251219_add_invoice_balance"
branch_labels = None
depends_on = None


def upgrade():
    # Add cit_amount column to monthly_tax_reports
    op.add_column("monthly_tax_reports", sa.Column("cit_amount", sa.Numeric(15, 2), nullable=True, server_default="0"))


def downgrade():
    op.drop_column("monthly_tax_reports", "cit_amount")
