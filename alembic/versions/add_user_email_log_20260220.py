"""Add user_email_log table for lifecycle drip emails

Tracks which lifecycle emails have been sent to each user so we never
send duplicates (e.g., welcome Day 0, Day 1, Day 3).

Revision ID: user_email_log_20260220
Revises: user_pro_override_20260220
Create Date: 2026-02-20
"""

import sqlalchemy as sa

from alembic import op

revision = "user_email_log_20260220"
down_revision = "user_pro_override_20260220"


def upgrade():
    op.create_table(
        "user_email_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("email_type", sa.String(60), nullable=False, index=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "email_type", name="uq_user_email_log_user_type"),
    )


def downgrade():
    op.drop_table("user_email_log")
