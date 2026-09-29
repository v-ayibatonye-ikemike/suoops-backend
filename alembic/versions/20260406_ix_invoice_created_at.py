"""add index on invoice.created_at for dashboard date sorting

Revision ID: 20260406_ix_invoice_created_at
Create Date: 2026-04-06
"""

from alembic import op

revision = "20260406_ix_invoice_created_at"
down_revision = "add_testimonial_20260312"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_invoice_created_at",
        "invoice",
        ["created_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_created_at", table_name="invoice")
