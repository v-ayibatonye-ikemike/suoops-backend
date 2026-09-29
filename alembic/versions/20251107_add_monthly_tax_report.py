"""add monthly tax report table

Revision ID: 20251107_add_monthly_tax_report
Revises: 0017_rename_nrs_submission_id
Create Date: 2025-11-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251107_add_monthly_tax_report"
down_revision = "0017_rename_nrs_submission_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _table_exists("monthly_tax_reports"):
        op.create_table(
            "monthly_tax_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False, index=True),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("assessable_profit", sa.Numeric(15, 2), server_default="0"),
            sa.Column("levy_amount", sa.Numeric(15, 2), server_default="0"),
            sa.Column("vat_collected", sa.Numeric(15, 2), server_default="0"),
            sa.Column("taxable_sales", sa.Numeric(15, 2), server_default="0"),
            sa.Column("zero_rated_sales", sa.Numeric(15, 2), server_default="0"),
            sa.Column("exempt_sales", sa.Numeric(15, 2), server_default="0"),
            sa.Column("pdf_url", sa.String(500), nullable=True),
            sa.Column("generated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_monthly_tax_reports_user_period", "monthly_tax_reports", ["user_id", "year", "month"], unique=False
        )


def downgrade() -> None:
    if _table_exists("monthly_tax_reports"):
        op.drop_index("ix_monthly_tax_reports_user_period", table_name="monthly_tax_reports")
        op.drop_table("monthly_tax_reports")


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()
