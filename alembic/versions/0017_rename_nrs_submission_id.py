"""Rename nrs_submission_id to firs_submission_id

Revision ID: 0017_rename_nrs_submission_id
Revises: 0016_rename_nrs_to_firs_fields
Create Date: 2025-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_rename_nrs_submission_id"
down_revision = "0016_rename_nrs_to_firs_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vat_returns") as batch_op:
        batch_op.alter_column("nrs_submission_id", new_column_name="firs_submission_id")


def downgrade() -> None:
    with op.batch_alter_table("vat_returns") as batch_op:
        batch_op.alter_column("firs_submission_id", new_column_name="nrs_submission_id")
