"""Add persisted expense integrity flag.

Revision ID: 20260728_expense_integrity
Revises: 20260727_escrow_indexes
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260728_expense_integrity"
down_revision = "20260727_escrow_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice",
        sa.Column("expense_flag_reason", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_invoice_expense_flag_reason",
        "invoice",
        ["expense_flag_reason"],
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_expense_flag_reason", table_name="invoice")
    op.drop_column("invoice", "expense_flag_reason")
