"""add email_enc column for encrypted email storage

Revision ID: 20251110_01
Revises: 20251109_add_paid_at_receipt
Create Date: 2025-11-10

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251110_01"
down_revision = "20251109_add_paid_at_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable encrypted email column alongside existing email
    op.add_column("user", sa.Column("email_enc", sa.String(length=512), nullable=True))
    op.create_index("ix_user_email_enc", "user", ["email_enc"], unique=False)
    # Optionally backfill: copy current email values that look encrypted (heuristic) or leave null.
    # For now we leave null to allow application dual-write logic to populate.


def downgrade() -> None:
    op.drop_index("ix_user_email_enc", table_name="user")
    op.drop_column("user", "email_enc")
