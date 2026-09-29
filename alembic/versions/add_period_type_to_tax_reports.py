"""Add period_type and date range to tax reports

Revision ID: add_period_type_tax
Revises:
Create Date: 2025-01-26

This migration adds support for multiple time aggregations (day, week, month, year)
to the tax reporting system by adding period_type and date range columns.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "add_period_type_tax"
down_revision = "20251110_03_add_role"
branch_labels = None
depends_on = None


def upgrade():
    # Add new columns to monthly_tax_reports table
    op.add_column("monthly_tax_reports", sa.Column("period_type", sa.String(10), nullable=True, server_default="month"))
    op.add_column("monthly_tax_reports", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("monthly_tax_reports", sa.Column("end_date", sa.Date(), nullable=True))

    # Populate start_date and end_date for existing monthly reports
    # This uses PostgreSQL's date functions
    op.execute("""
        UPDATE monthly_tax_reports
        SET start_date = make_date(year, month, 1),
            end_date = (make_date(year, month, 1) + interval '1 month' - interval '1 day')::date
        WHERE start_date IS NULL AND year IS NOT NULL AND month IS NOT NULL
    """)

    # Make year and month nullable for non-monthly reports
    op.alter_column("monthly_tax_reports", "year", nullable=True)
    op.alter_column("monthly_tax_reports", "month", nullable=True)

    # Add check constraint for period_type values
    op.create_check_constraint(
        "ck_period_type", "monthly_tax_reports", "period_type IN ('day', 'week', 'month', 'year')"
    )

    # Add index for faster queries by period_type and date range
    op.create_index(
        "ix_tax_reports_period_dates", "monthly_tax_reports", ["user_id", "period_type", "start_date", "end_date"]
    )


def downgrade():
    # Remove the new columns and constraints
    op.drop_index("ix_tax_reports_period_dates", table_name="monthly_tax_reports")
    op.drop_constraint("ck_period_type", "monthly_tax_reports", type_="check")

    # Restore year and month as NOT NULL before dropping new columns
    op.alter_column("monthly_tax_reports", "year", nullable=False)
    op.alter_column("monthly_tax_reports", "month", nullable=False)

    op.drop_column("monthly_tax_reports", "end_date")
    op.drop_column("monthly_tax_reports", "start_date")
    op.drop_column("monthly_tax_reports", "period_type")
