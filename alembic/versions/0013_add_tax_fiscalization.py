"""Add tax and fiscalization tables for NRS 2026 compliance

Revision ID: 0013_add_tax_fiscalization
Revises: 0012_add_email_to_user
Create Date: 2025-11-07

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_add_tax_fiscalization"
down_revision = "0012_add_email_to_user"
branch_labels = None
depends_on = None


def upgrade():
    # Create tax_profiles table
    op.create_table(
        "tax_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("business_size", sa.String(length=20), nullable=True),
        sa.Column("annual_turnover", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("fixed_assets", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("tin", sa.String(length=20), nullable=True),
        sa.Column("vat_registered", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("vat_registration_number", sa.String(length=20), nullable=True),
        sa.Column("last_vat_return", sa.DateTime(), nullable=True),
        sa.Column("last_compliance_check", sa.DateTime(), nullable=True),
        sa.Column("nrs_registered", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("nrs_merchant_id", sa.String(length=50), nullable=True),
        sa.Column("nrs_api_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(op.f("ix_tax_profiles_tin"), "tax_profiles", ["tin"], unique=False)

    # Create fiscal_invoices table
    op.create_table(
        "fiscal_invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("fiscal_code", sa.String(length=100), nullable=False),
        sa.Column("fiscal_signature", sa.String(length=500), nullable=False),
        sa.Column("qr_code_data", sa.String(length=5000), nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("vat_rate", sa.Float(), nullable=True, server_default="7.5"),
        sa.Column("vat_amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("zero_rated_amount", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("zero_rated_items", sa.JSON(), nullable=True),
        sa.Column("transmitted_at", sa.DateTime(), nullable=True),
        sa.Column("nrs_response", sa.JSON(), nullable=True),
        sa.Column("nrs_validation_status", sa.String(length=20), nullable=True, server_default="pending"),
        sa.Column("nrs_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoice.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fiscal_code"),
        sa.UniqueConstraint("invoice_id"),
    )
    op.create_index(op.f("ix_fiscal_invoices_fiscal_code"), "fiscal_invoices", ["fiscal_code"], unique=True)

    # Create vat_returns table
    op.create_table(
        "vat_returns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tax_period", sa.String(length=7), nullable=False),
        sa.Column("start_date", sa.DateTime(), nullable=False),
        sa.Column("end_date", sa.DateTime(), nullable=False),
        sa.Column("output_vat", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("input_vat", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("net_vat", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("zero_rated_sales", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("exempt_sales", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0"),
        sa.Column("total_invoices", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("fiscalized_invoices", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=True, server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("nrs_submission_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vat_returns_tax_period"), "vat_returns", ["tax_period"], unique=False)

    # Add VAT and fiscalization fields to invoices table
    op.add_column("invoice", sa.Column("vat_rate", sa.Float(), nullable=True, server_default="7.5"))
    op.add_column(
        "invoice", sa.Column("vat_amount", sa.Numeric(precision=15, scale=2), nullable=True, server_default="0")
    )
    op.add_column("invoice", sa.Column("vat_category", sa.String(length=20), nullable=True, server_default="standard"))
    op.add_column("invoice", sa.Column("is_fiscalized", sa.Boolean(), nullable=True, server_default="false"))
    op.add_column("invoice", sa.Column("fiscal_code", sa.String(length=100), nullable=True))
    op.create_index(op.f("ix_invoice_fiscal_code"), "invoice", ["fiscal_code"], unique=True)


def downgrade():
    # Drop indexes and columns from invoice table
    op.drop_index(op.f("ix_invoice_fiscal_code"), table_name="invoice")
    op.drop_column("invoice", "fiscal_code")
    op.drop_column("invoice", "is_fiscalized")
    op.drop_column("invoice", "vat_category")
    op.drop_column("invoice", "vat_amount")
    op.drop_column("invoice", "vat_rate")

    # Drop vat_returns table
    op.drop_index(op.f("ix_vat_returns_tax_period"), table_name="vat_returns")
    op.drop_table("vat_returns")

    # Drop fiscal_invoices table
    op.drop_index(op.f("ix_fiscal_invoices_fiscal_code"), table_name="fiscal_invoices")
    op.drop_table("fiscal_invoices")

    # Drop tax_profiles table
    op.drop_index(op.f("ix_tax_profiles_tin"), table_name="tax_profiles")
    op.drop_table("tax_profiles")
