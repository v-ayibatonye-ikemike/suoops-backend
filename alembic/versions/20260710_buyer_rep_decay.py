"""buyer reputation flag decay timestamp

Revision ID: 20260710_buyer_rep_decay
Revises: 20260710_escrow_settle_at
Create Date: 2026-07-10

Adds ``buyer_reputation.last_false_dispute_at`` so a buyer's abuse flag can decay
after a quiet period (no new admin-ruled false disputes), instead of being
permanent.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260710_buyer_rep_decay"
down_revision = "20260710_escrow_settle_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "buyer_reputation",
        sa.Column("last_false_dispute_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("buyer_reputation", "last_false_dispute_at")
