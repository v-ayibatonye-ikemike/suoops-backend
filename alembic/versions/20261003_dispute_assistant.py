"""Record admin actors for AI-assisted dispute review.

Revision ID: 20261003_dispute_ai
Revises: 20261003_storefront_ai
Create Date: 2026-10-03
"""

import sqlalchemy as sa

from alembic import op

revision = "20261003_dispute_ai"
down_revision = "20261003_storefront_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("ai_usage_event", "actor_user_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("ai_usage_event", sa.Column("actor_admin_user_id", sa.Integer(), nullable=True))
    op.add_column(
        "ai_usage_event",
        sa.Column(
            "counts_toward_quota",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_foreign_key(
        "fk_ai_usage_event_actor_admin_user_id",
        "ai_usage_event",
        "admin_users",
        ["actor_admin_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_ai_usage_event_actor_admin_user_id",
        "ai_usage_event",
        ["actor_admin_user_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_ai_usage_event_exactly_one_actor",
        "ai_usage_event",
        "(actor_user_id IS NOT NULL) <> (actor_admin_user_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_usage_event_exactly_one_actor", "ai_usage_event", type_="check")
    op.execute(
        "UPDATE ai_usage_event SET actor_user_id = data_owner_id "
        "WHERE actor_user_id IS NULL AND actor_admin_user_id IS NOT NULL"
    )
    op.drop_index("ix_ai_usage_event_actor_admin_user_id", table_name="ai_usage_event")
    op.drop_constraint(
        "fk_ai_usage_event_actor_admin_user_id",
        "ai_usage_event",
        type_="foreignkey",
    )
    op.drop_column("ai_usage_event", "counts_toward_quota")
    op.drop_column("ai_usage_event", "actor_admin_user_id")
    op.alter_column("ai_usage_event", "actor_user_id", existing_type=sa.Integer(), nullable=False)
