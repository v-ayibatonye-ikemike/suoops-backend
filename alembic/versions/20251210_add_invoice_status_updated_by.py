"""Add invoice status_updated_by tracking

Revision ID: 20251210_status_updated_by
Revises: 20251210_add_invoice_created_by
Create Date: 2025-12-10

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251210_status_updated_by"
down_revision = "20251210_add_invoice_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add status_updated_by_user_id column
    op.add_column(
        "invoice",
        sa.Column("status_updated_by_user_id", sa.Integer(), nullable=True),
    )

    # Add status_updated_at column
    op.add_column(
        "invoice",
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Add index for status_updated_by_user_id
    op.create_index(
        "ix_invoice_status_updated_by_user_id",
        "invoice",
        ["status_updated_by_user_id"],
    )

    # Add foreign key constraint
    op.create_foreign_key(
        "fk_invoice_status_updated_by_user_id",
        "invoice",
        "user",
        ["status_updated_by_user_id"],
        ["id"],
    )


def downgrade() -> None:
    # Remove foreign key constraint
    op.drop_constraint("fk_invoice_status_updated_by_user_id", "invoice", type_="foreignkey")

    # Remove index
    op.drop_index("ix_invoice_status_updated_by_user_id", table_name="invoice")

    # Remove columns
    op.drop_column("invoice", "status_updated_at")
    op.drop_column("invoice", "status_updated_by_user_id")
