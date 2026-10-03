"""Add Commerce Copilot briefing cache and proposed actions.

Revision ID: 20261003_copilot_mvp
Revises: 20261003_ai_usage
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261003_copilot_mvp"
down_revision = "20261003_ai_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_copilot_briefing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data_owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("briefing_date", sa.Date(), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("headline", sa.String(length=180), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("ai_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generation_notice", sa.String(length=180), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("data_owner_id", "briefing_date", name="uq_ai_briefing_owner_date"),
    )
    op.create_index("ix_ai_copilot_briefing_data_owner_id", "ai_copilot_briefing", ["data_owner_id"])
    op.create_index("ix_ai_copilot_briefing_briefing_date", "ai_copilot_briefing", ["briefing_date"])

    op.create_table(
        "ai_proposed_action",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("data_owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("proposed_by_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("action_type", sa.String(length=60), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("action_url", sa.String(length=300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=140), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.UniqueConstraint("data_owner_id", "dedupe_key", name="uq_ai_action_owner_dedupe"),
    )
    op.create_index("ix_ai_proposed_action_public_id", "ai_proposed_action", ["public_id"], unique=True)
    op.create_index("ix_ai_proposed_action_data_owner_id", "ai_proposed_action", ["data_owner_id"])
    op.create_index("ix_ai_proposed_action_action_type", "ai_proposed_action", ["action_type"])
    op.create_index("ix_ai_proposed_action_status", "ai_proposed_action", ["status"])
    op.create_index(
        "ix_ai_action_owner_status_created",
        "ai_proposed_action",
        ["data_owner_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_action_owner_status_created", table_name="ai_proposed_action")
    op.drop_index("ix_ai_proposed_action_status", table_name="ai_proposed_action")
    op.drop_index("ix_ai_proposed_action_action_type", table_name="ai_proposed_action")
    op.drop_index("ix_ai_proposed_action_data_owner_id", table_name="ai_proposed_action")
    op.drop_index("ix_ai_proposed_action_public_id", table_name="ai_proposed_action")
    op.drop_table("ai_proposed_action")
    op.drop_index("ix_ai_copilot_briefing_briefing_date", table_name="ai_copilot_briefing")
    op.drop_index("ix_ai_copilot_briefing_data_owner_id", table_name="ai_copilot_briefing")
    op.drop_table("ai_copilot_briefing")
