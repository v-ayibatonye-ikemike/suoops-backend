"""Add invoice_reminder_log table for tracking payment reminders

Prevents duplicate reminders per invoice per tier. Tracks both
customer-facing and business-owner reminders.

Revision ID: invoice_reminder_log_20260223
Revises: user_email_log_20260220
Create Date: 2026-02-23
"""

import sqlalchemy as sa

from alembic import op

revision = "invoice_reminder_log_20260223"
down_revision = "user_email_log_20260220"


def upgrade():
    op.create_table(
        "invoice_reminder_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "invoice_id",
            sa.Integer(),
            sa.ForeignKey("invoice.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "reminder_type",
            sa.String(30),
            nullable=False,
        ),  # customer_pre_due, customer_due_today, customer_overdue_1d, etc.
        sa.Column(
            "channel",
            sa.String(20),
            nullable=False,
        ),  # whatsapp, email
        sa.Column(
            "recipient",
            sa.String(255),
            nullable=False,
        ),  # phone or email used
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Prevent duplicate reminders of the same type to same recipient
        sa.UniqueConstraint(
            "invoice_id",
            "reminder_type",
            "channel",
            name="uq_invoice_reminder_type_channel",
        ),
    )


def downgrade():
    op.drop_table("invoice_reminder_log")
