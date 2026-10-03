"""Add database-backed AI governance controls and feedback.

Revision ID: 20261003_ai_governance
Revises: 20261003_dispute_ai
Create Date: 2026-10-03
"""

import sqlalchemy as sa

from alembic import op

revision = "20261003_ai_governance"
down_revision = "20261003_dispute_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_feature_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rollout_percent", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("allowlisted_owner_ids", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("updated_by_admin_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["updated_by_admin_user_id"], ["admin_users.id"]),
        sa.CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_ai_feature_control_rollout_percent",
        ),
        sa.UniqueConstraint("feature"),
    )
    op.create_index("ix_ai_feature_control_feature", "ai_feature_control", ["feature"], unique=True)

    op.create_table(
        "ai_tenant_preference",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data_owner_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("feature_overrides", sa.JSON(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["data_owner_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["user.id"]),
        sa.UniqueConstraint("data_owner_id"),
    )
    op.create_index(
        "ix_ai_tenant_preference_data_owner_id",
        "ai_tenant_preference",
        ["data_owner_id"],
        unique=True,
    )

    op.create_table(
        "ai_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data_owner_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("feature", sa.String(length=80), nullable=False),
        sa.Column("sentiment", sa.String(length=10), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("context_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["data_owner_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "sentiment IN ('positive', 'negative')",
            name="ck_ai_feedback_sentiment",
        ),
    )
    op.create_index("ix_ai_feedback_data_owner_id", "ai_feedback", ["data_owner_id"], unique=False)
    op.create_index("ix_ai_feedback_actor_user_id", "ai_feedback", ["actor_user_id"], unique=False)
    op.create_index("ix_ai_feedback_created_at", "ai_feedback", ["created_at"], unique=False)
    op.create_index("ix_ai_feedback_feature_created", "ai_feedback", ["feature", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_feedback_feature_created", table_name="ai_feedback")
    op.drop_index("ix_ai_feedback_created_at", table_name="ai_feedback")
    op.drop_index("ix_ai_feedback_actor_user_id", table_name="ai_feedback")
    op.drop_index("ix_ai_feedback_data_owner_id", table_name="ai_feedback")
    op.drop_table("ai_feedback")
    op.drop_index("ix_ai_tenant_preference_data_owner_id", table_name="ai_tenant_preference")
    op.drop_table("ai_tenant_preference")
    op.drop_index("ix_ai_feature_control_feature", table_name="ai_feature_control")
    op.drop_table("ai_feature_control")
