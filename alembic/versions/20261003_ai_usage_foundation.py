"""Add tenant-scoped AI usage ledger.

Revision ID: 20261003_ai_usage
Revises: 20260925_social_marketing
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261003_ai_usage"
down_revision = "20260925_social_marketing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("data_owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("feature", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ai_usage_event_operation_id", "ai_usage_event", ["operation_id"], unique=True)
    op.create_index("ix_ai_usage_event_data_owner_id", "ai_usage_event", ["data_owner_id"])
    op.create_index("ix_ai_usage_event_actor_user_id", "ai_usage_event", ["actor_user_id"])
    op.create_index("ix_ai_usage_event_feature", "ai_usage_event", ["feature"])
    op.create_index("ix_ai_usage_event_status", "ai_usage_event", ["status"])
    op.create_index("ix_ai_usage_event_created_at", "ai_usage_event", ["created_at"])
    op.create_index("ix_ai_usage_owner_created", "ai_usage_event", ["data_owner_id", "created_at"])
    op.create_index(
        "ix_ai_usage_owner_status_created",
        "ai_usage_event",
        ["data_owner_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_owner_status_created", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_owner_created", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_created_at", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_status", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_feature", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_actor_user_id", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_data_owner_id", table_name="ai_usage_event")
    op.drop_index("ix_ai_usage_event_operation_id", table_name="ai_usage_event")
    op.drop_table("ai_usage_event")
