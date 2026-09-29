"""Durable audit_log table (survives redeploys) with hash chain

Revision ID: 20260720_audit_log
Revises: 20260712_delivery_status
Create Date: 2026-07-20

The file-based audit log (``storage/audit.log``) lives on Render's ephemeral
disk and is wiped on every deploy. This adds a durable ``audit_log`` table so
security/compliance events persist and are queryable. Each row carries
``entry_hash = sha256(prev_hash + event)`` for cheap tamper-evidence.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260720_audit_log"
down_revision = "20260712_delivery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="success",
            nullable=False,
        ),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_entry_hash", "audit_log", ["entry_hash"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_entry_hash", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
