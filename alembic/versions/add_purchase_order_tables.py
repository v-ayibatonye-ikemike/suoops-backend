"""add purchase_order and purchase_order_line tables

Revision ID: add_purchase_order_tables
Revises: add_payout_bank_fields
Create Date: 2026-01-08

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_purchase_order_tables"
down_revision: Union[str, None] = "add_payout_bank_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create purchase_order table
    op.create_table(
        "purchase_order",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("order_number", sa.String(50), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "ordered", "received", "cancelled", name="purchaseorderstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("total_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("auto_generated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("trigger_invoice_id", sa.String(50), nullable=True),
        sa.Column("order_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expected_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_order_user_id", "purchase_order", ["user_id"], unique=False)
    op.create_index("ix_purchase_order_supplier_id", "purchase_order", ["supplier_id"], unique=False)
    op.create_index("ix_purchase_order_order_number", "purchase_order", ["order_number"], unique=True)
    op.create_index("ix_purchase_order_status", "purchase_order", ["status"], unique=False)
    op.create_index("ix_purchase_order_user_status", "purchase_order", ["user_id", "status"], unique=False)

    # Create purchase_order_line table
    op.create_table(
        "purchase_order_line",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(15, 2), nullable=True),
        sa.Column("total_cost", sa.Numeric(15, 2), nullable=True),
        sa.Column("quantity_received", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_order.id"],
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_purchase_order_line_purchase_order_id", "purchase_order_line", ["purchase_order_id"], unique=False
    )
    op.create_index("ix_purchase_order_line_product_id", "purchase_order_line", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_table("purchase_order_line")
    op.drop_table("purchase_order")
    op.execute("DROP TYPE IF EXISTS purchaseorderstatus")
