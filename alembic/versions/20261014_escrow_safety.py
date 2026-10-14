"""Persist reviewed escrow release approval separately from buyer confirmation."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_escrow_safety"
down_revision = "20261014_ongoing_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_order_escrow",
        sa.Column("release_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_order_escrow",
        sa.Column("transfer_not_submitted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("storefront_order_escrow", "transfer_not_submitted")
    op.drop_column("storefront_order_escrow", "release_approved_at")
