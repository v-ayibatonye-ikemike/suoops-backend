"""Add merchant-approved collection reminder drafts.

Revision ID: 20261003_collections
Revises: 20261003_copilot_mvp
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261003_collections"
down_revision = "20261003_copilot_mvp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_collection_draft",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("data_owner_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("recipient_masked", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=180), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("priority_score", sa.Integer(), nullable=False),
        sa.Column("priority_level", sa.String(length=20), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.String(length=600), nullable=False),
        sa.Column("ai_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dedupe_key", sa.String(length=140), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.UniqueConstraint("data_owner_id", "dedupe_key", name="uq_ai_collection_owner_dedupe"),
    )
    op.create_index("ix_ai_collection_draft_public_id", "ai_collection_draft", ["public_id"], unique=True)
    op.create_index("ix_ai_collection_draft_data_owner_id", "ai_collection_draft", ["data_owner_id"])
    op.create_index("ix_ai_collection_draft_invoice_id", "ai_collection_draft", ["invoice_id"])
    op.create_index("ix_ai_collection_draft_status", "ai_collection_draft", ["status"])
    op.create_index(
        "ix_ai_collection_owner_status_created",
        "ai_collection_draft",
        ["data_owner_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_collection_owner_status_created", table_name="ai_collection_draft")
    op.drop_index("ix_ai_collection_draft_status", table_name="ai_collection_draft")
    op.drop_index("ix_ai_collection_draft_invoice_id", table_name="ai_collection_draft")
    op.drop_index("ix_ai_collection_draft_data_owner_id", table_name="ai_collection_draft")
    op.drop_index("ix_ai_collection_draft_public_id", table_name="ai_collection_draft")
    op.drop_table("ai_collection_draft")
