"""user terms_accepted_at (T&C gating at signup)

Revision ID: 20260708_terms_accepted
Revises: 20260708_escrow_charge_ref
Create Date: 2026-07-08

Adds ``user.terms_accepted_at`` — timestamp of when the business accepted the
Terms & Conditions (including the buyer-protection / escrow policy) at signup.
NULL for legacy accounts created before T&C gating.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_terms_accepted"
down_revision = "20260708_escrow_charge_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "terms_accepted_at")
