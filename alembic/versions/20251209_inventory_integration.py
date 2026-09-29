"""Add inventory workflow integration fields

Revision ID: 20251209_inventory_integration
Revises: 20251209_add_inventory
Create Date: 2025-12-09

This migration adds:
- product_id to invoiceline for inventory-invoice linking
- COGS fields to monthly_tax_reports for tax integration
- supplier_id to stock_movement for purchase tracking
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251209_inventory_integration"
down_revision = "20251209_add_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add product_id to invoiceline for inventory linking
    op.add_column("invoiceline", sa.Column("product_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_invoiceline_product_id", "invoiceline", "product", ["product_id"], ["id"])
    op.create_index("ix_invoiceline_product_id", "invoiceline", ["product_id"], unique=False)

    # Add COGS fields to monthly_tax_reports for inventory-tax integration
    op.add_column(
        "monthly_tax_reports",
        sa.Column("cogs_amount", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
    )
    op.add_column(
        "monthly_tax_reports",
        sa.Column("inventory_purchases", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
    )
    op.add_column(
        "monthly_tax_reports",
        sa.Column("inventory_value", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
    )

    # Add supplier_id to stock_movement for purchase tracking
    op.add_column("stock_movement", sa.Column("supplier_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_stock_movement_supplier_id", "stock_movement", "supplier", ["supplier_id"], ["id"])


def downgrade() -> None:
    # Remove supplier_id from stock_movement
    op.drop_constraint("fk_stock_movement_supplier_id", "stock_movement", type_="foreignkey")
    op.drop_column("stock_movement", "supplier_id")

    # Remove COGS fields from monthly_tax_reports
    op.drop_column("monthly_tax_reports", "inventory_value")
    op.drop_column("monthly_tax_reports", "inventory_purchases")
    op.drop_column("monthly_tax_reports", "cogs_amount")

    # Remove product_id from invoiceline
    op.drop_index("ix_invoiceline_product_id", table_name="invoiceline")
    op.drop_constraint("fk_invoiceline_product_id", "invoiceline", type_="foreignkey")
    op.drop_column("invoiceline", "product_id")
