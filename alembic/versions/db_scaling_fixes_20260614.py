"""Add missing indexes and log cleanup for scaling.

New indexes:
- ix_invoice_paid_at — weekly summary queries filter by paid_at
- ix_reminder_log_sent_at — cleanup queries, dedup by date
- ix_email_log_sent_at — cleanup queries, dedup by date

Revision ID: db_scaling_fixes_20260614
Revises: scalability_indexes_20260614
"""

from alembic import op

revision = "db_scaling_fixes_20260614"
down_revision = "scalability_indexes_20260614"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_invoice_paid_at",
        "invoice",
        ["paid_at"],
        postgresql_where="paid_at IS NOT NULL",
    )
    op.create_index(
        "ix_reminder_log_sent_at",
        "invoice_reminder_log",
        ["sent_at"],
    )
    op.create_index(
        "ix_email_log_sent_at",
        "user_email_log",
        ["sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_log_sent_at", table_name="user_email_log")
    op.drop_index("ix_reminder_log_sent_at", table_name="invoice_reminder_log")
    op.drop_index("ix_invoice_paid_at", table_name="invoice")
