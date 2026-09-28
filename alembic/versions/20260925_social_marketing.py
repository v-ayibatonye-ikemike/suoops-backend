"""Add social media auto-promotion: opt-in flags + social_posts tracking table.

Revision ID: 20260925_social_marketing
Revises: 20260923_cac_verification
Create Date: 2026-09-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260925_social_marketing"
down_revision = "20260923_cac_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "social_promotion_opt_in", sa.Boolean(), nullable=True, server_default=sa.false()
        ),
    )
    op.add_column(
        "product",
        sa.Column(
            "exclude_from_social", sa.Boolean(), nullable=True, server_default=sa.false()
        ),
    )
    op.create_table(
        "social_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("product.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="posted"
        ),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(length=500), nullable=False),
        sa.Column("utm_link", sa.String(length=600), nullable=False),
        sa.Column("external_post_id", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_social_posts_user_created", "social_posts", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_social_posts_product_created", "social_posts", ["product_id", "created_at"]
    )
    op.create_index("ix_social_posts_product_id", "social_posts", ["product_id"])
    op.create_index("ix_social_posts_user_id", "social_posts", ["user_id"])
    op.create_index("ix_social_posts_platform", "social_posts", ["platform"])
    op.create_index("ix_social_posts_created_at", "social_posts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_social_posts_created_at", table_name="social_posts")
    op.drop_index("ix_social_posts_platform", table_name="social_posts")
    op.drop_index("ix_social_posts_user_id", table_name="social_posts")
    op.drop_index("ix_social_posts_product_id", table_name="social_posts")
    op.drop_index("ix_social_posts_product_created", table_name="social_posts")
    op.drop_index("ix_social_posts_user_created", table_name="social_posts")
    op.drop_table("social_posts")
    op.drop_column("product", "exclude_from_social")
    op.drop_column("user", "social_promotion_opt_in")
