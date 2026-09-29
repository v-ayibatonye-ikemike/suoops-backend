"""storefront upgrade: location, hours, announcement, delivery, analytics, reviews, notify

Revision ID: 20260707_storefront_upgrade
Revises: 20260706_storefront_description
Create Date: 2026-07-07

Adds the storefront "profile" fields (address, business hours, announcement
banner, delivery/pickup + fee, custom domain, view counter) to ``user`` and
two small supporting tables:
  - ``storefront_stock_notification`` : "notify me when back in stock" waitlist
  - ``storefront_review``             : customer reviews (gated to paid buyers)
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260707_storefront_upgrade"
down_revision = "20260706_storefront_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── User: storefront profile fields ──
    op.add_column("user", sa.Column("storefront_address", sa.String(length=200), nullable=True))
    op.add_column("user", sa.Column("storefront_city", sa.String(length=80), nullable=True))
    op.add_column("user", sa.Column("storefront_state", sa.String(length=80), nullable=True))
    # Weekly opening hours: {"0": {"open": "09:00", "close": "18:00"}, ...} (0=Mon).
    op.add_column("user", sa.Column("storefront_hours", sa.JSON(), nullable=True))
    op.add_column("user", sa.Column("storefront_announcement", sa.String(length=200), nullable=True))
    op.add_column(
        "user",
        sa.Column("storefront_views", sa.Integer(), server_default="0", nullable=False),
    )

    # stock "notify me" waitlist
    op.create_table(
        "storefront_stock_notification",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, index=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("product.id"), nullable=False, index=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("notified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Customer reviews (gated to paid buyers) ──
    op.create_table(
        "storefront_review",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, index=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customer.id"), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("reviewer_name", sa.String(length=100), nullable=True),
        sa.Column("approved", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("storefront_review")
    op.drop_table("storefront_stock_notification")
    op.drop_column("user", "storefront_views")
    op.drop_column("user", "storefront_announcement")
    op.drop_column("user", "storefront_hours")
    op.drop_column("user", "storefront_state")
    op.drop_column("user", "storefront_city")
    op.drop_column("user", "storefront_address")
