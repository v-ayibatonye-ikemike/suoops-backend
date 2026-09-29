"""Add email_suppression table for SES bounce/complaint handling.

Revision ID: email_suppression_20260627
Revises: fix_comm_pct_20260616
Create Date: 2026-06-27
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "email_suppression_20260627"
down_revision = "fix_comm_pct_20260616"


def upgrade() -> None:
    op.create_table(
        "email_suppression",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="ses", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_email_suppression_email",
        "email_suppression",
        ["email"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_email_suppression_email", table_name="email_suppression")
    op.drop_table("email_suppression")
