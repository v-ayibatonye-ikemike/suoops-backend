"""Rename NRS registration fields to FIRS equivalents

Revision ID: 0016_rename_nrs_to_firs_fields
Revises: 0015_add_index_invoice_issuer_id
Create Date: 2025-11-07
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_rename_nrs_to_firs_fields"
down_revision: Union[str, None] = "0015_add_index_invoice_issuer_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tax profile column renames
    with op.batch_alter_table("tax_profiles") as batch_op:
        # Use existence checks defensively (SQLite compatibility) - errors ignored if column absent
        try:
            batch_op.alter_column("nrs_registered", new_column_name="firs_registered")
        except Exception:
            pass
        try:
            batch_op.alter_column("nrs_merchant_id", new_column_name="firs_merchant_id")
        except Exception:
            pass
        try:
            batch_op.alter_column("nrs_api_key", new_column_name="firs_api_key")
        except Exception:
            pass

    # Fiscal invoice transmission tracking fields
    with op.batch_alter_table("fiscal_invoices") as batch_op:
        try:
            batch_op.alter_column("nrs_response", new_column_name="firs_response")
        except Exception:
            pass
        try:
            batch_op.alter_column("nrs_validation_status", new_column_name="firs_validation_status")
        except Exception:
            pass
        try:
            batch_op.alter_column("nrs_transaction_id", new_column_name="firs_transaction_id")
        except Exception:
            pass


def downgrade() -> None:
    # Revert column names
    with op.batch_alter_table("tax_profiles") as batch_op:
        try:
            batch_op.alter_column("firs_registered", new_column_name="nrs_registered")
        except Exception:
            pass
        try:
            batch_op.alter_column("firs_merchant_id", new_column_name="nrs_merchant_id")
        except Exception:
            pass
        try:
            batch_op.alter_column("firs_api_key", new_column_name="nrs_api_key")
        except Exception:
            pass

    with op.batch_alter_table("fiscal_invoices") as batch_op:
        try:
            batch_op.alter_column("firs_response", new_column_name="nrs_response")
        except Exception:
            pass
        try:
            batch_op.alter_column("firs_validation_status", new_column_name="nrs_validation_status")
        except Exception:
            pass
        try:
            batch_op.alter_column("firs_transaction_id", new_column_name="nrs_transaction_id")
        except Exception:
            pass
