"""add ON DELETE SET NULL to invoice user references

Revision ID: invoice_user_set_null
Revises: add_purchase_order_tables
Create Date: 2026-01-08

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "invoice_user_set_null"
down_revision: Union[str, None] = "add_purchase_order_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop existing foreign key constraints
    op.drop_constraint("fk_invoice_created_by_user_id", "invoice", type_="foreignkey")
    op.drop_constraint("fk_invoice_status_updated_by_user_id", "invoice", type_="foreignkey")

    # Re-create with ON DELETE SET NULL
    op.create_foreign_key(
        "fk_invoice_created_by_user_id", "invoice", "user", ["created_by_user_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_invoice_status_updated_by_user_id",
        "invoice",
        "user",
        ["status_updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Drop constraints with SET NULL
    op.drop_constraint("fk_invoice_created_by_user_id", "invoice", type_="foreignkey")
    op.drop_constraint("fk_invoice_status_updated_by_user_id", "invoice", type_="foreignkey")

    # Re-create without ON DELETE clause
    op.create_foreign_key("fk_invoice_created_by_user_id", "invoice", "user", ["created_by_user_id"], ["id"])
    op.create_foreign_key(
        "fk_invoice_status_updated_by_user_id", "invoice", "user", ["status_updated_by_user_id"], ["id"]
    )
