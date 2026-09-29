"""add paid_at and receipt_pdf_url to invoice

Revision ID: 20251109_add_paid_at_receipt
Revises: 20251109_invoice_status_len
Create Date: 2025-11-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251109_add_paid_at_receipt"
down_revision = "20251109_invoice_status_len"
branch_labels = None
depends_on = None


def upgrade() -> None:  # noqa: D401
    op.add_column(
        "invoice",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoice",
        sa.Column("receipt_pdf_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:  # noqa: D401
    op.drop_column("invoice", "receipt_pdf_url")
    op.drop_column("invoice", "paid_at")
