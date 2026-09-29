"""Add inventory management tables

Revision ID: 20251209_add_inventory
Revises: 0035_oauth_tokens
Create Date: 2025-12-09

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251209_add_inventory"
down_revision = "0035_oauth_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create product_category table
    op.create_table(
        "product_category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_category_user_id", "product_category", ["user_id"], unique=False)
    op.create_index("ix_product_category_user_name", "product_category", ["user_id", "name"], unique=False)

    # Create product table
    op.create_table(
        "product",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("sku", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("barcode", sa.String(length=50), nullable=True),
        sa.Column("cost_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("selling_price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("quantity_in_stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reorder_level", sa.Integer(), server_default="10", nullable=False),
        sa.Column("reorder_quantity", sa.Integer(), server_default="20", nullable=False),
        sa.Column("unit", sa.String(length=20), server_default="pcs", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("track_stock", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["product_category.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_user_id", "product", ["user_id"], unique=False)
    op.create_index("ix_product_category_id", "product", ["category_id"], unique=False)
    op.create_index("ix_product_barcode", "product", ["barcode"], unique=False)
    op.create_index("ix_product_user_sku", "product", ["user_id", "sku"], unique=True)
    op.create_index("ix_product_user_name", "product", ["user_id", "name"], unique=False)

    # Create stock_movement table
    op.create_table(
        "stock_movement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "movement_type",
            sa.Enum(
                "purchase",
                "sale",
                "adjustment",
                "return_in",
                "return_out",
                "transfer",
                "opening",
                name="stockmovementtype",
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_before", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("total_cost", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("reference_type", sa.String(length=50), nullable=True),
        sa.Column("reference_id", sa.String(length=50), nullable=True),
        sa.Column("invoice_line_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ["invoice_line_id"],
            ["invoiceline.id"],
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_movement_user_id", "stock_movement", ["user_id"], unique=False)
    op.create_index("ix_stock_movement_product_id", "stock_movement", ["product_id"], unique=False)
    op.create_index("ix_stock_movement_type", "stock_movement", ["movement_type"], unique=False)
    op.create_index("ix_stock_movement_product_date", "stock_movement", ["product_id", "created_at"], unique=False)
    op.create_index("ix_stock_movement_user_date", "stock_movement", ["user_id", "created_at"], unique=False)

    # Create supplier table
    op.create_table(
        "supplier",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("contact_name", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supplier_user_id", "supplier", ["user_id"], unique=False)
    op.create_index("ix_supplier_user_name", "supplier", ["user_id", "name"], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order (respecting foreign keys)
    op.drop_index("ix_supplier_user_name", table_name="supplier")
    op.drop_index("ix_supplier_user_id", table_name="supplier")
    op.drop_table("supplier")

    op.drop_index("ix_stock_movement_user_date", table_name="stock_movement")
    op.drop_index("ix_stock_movement_product_date", table_name="stock_movement")
    op.drop_index("ix_stock_movement_type", table_name="stock_movement")
    op.drop_index("ix_stock_movement_product_id", table_name="stock_movement")
    op.drop_index("ix_stock_movement_user_id", table_name="stock_movement")
    op.drop_table("stock_movement")

    # Drop the enum type
    op.execute("DROP TYPE IF EXISTS stockmovementtype")

    op.drop_index("ix_product_user_name", table_name="product")
    op.drop_index("ix_product_user_sku", table_name="product")
    op.drop_index("ix_product_barcode", table_name="product")
    op.drop_index("ix_product_category_id", table_name="product")
    op.drop_index("ix_product_user_id", table_name="product")
    op.drop_table("product")

    op.drop_index("ix_product_category_user_name", table_name="product_category")
    op.drop_index("ix_product_category_user_id", table_name="product_category")
    op.drop_table("product_category")
