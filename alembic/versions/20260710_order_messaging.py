"""order-scoped storefront messaging + seller circumvention counter

Revision ID: 20260710_order_messaging
Revises: 20260708_escrow_charge_ref
Create Date: 2026-07-10

Adds the ``order_message`` table (guarded buyer/seller messaging tied to an
escrow) and ``user.circumvention_attempts`` (count of off-platform-pushing
messages; enough of them flags the seller for review).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_order_messaging"
down_revision = "20260708_delivery_buyer_rep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "circumvention_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "order_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "escrow_id",
            sa.Integer(),
            sa.ForeignKey("storefront_order_escrow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender_role", sa.String(length=10), nullable=False),
        sa.Column(
            "sender_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
        sa.Column("body_raw", sa.Text(), nullable=False),
        sa.Column("body_redacted", sa.Text(), nullable=False),
        sa.Column("flagged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("flag_reasons", sa.String(length=200), nullable=True),
        sa.Column("blocked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_message_escrow_id", "order_message", ["escrow_id"])
    op.create_index("ix_order_message_sender_user_id", "order_message", ["sender_user_id"])
    op.create_index("ix_order_message_flagged", "order_message", ["flagged"])
    op.create_index("ix_order_message_created_at", "order_message", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_order_message_created_at", table_name="order_message")
    op.drop_index("ix_order_message_flagged", table_name="order_message")
    op.drop_index("ix_order_message_sender_user_id", table_name="order_message")
    op.drop_index("ix_order_message_escrow_id", table_name="order_message")
    op.drop_table("order_message")
    op.drop_column("user", "circumvention_attempts")
