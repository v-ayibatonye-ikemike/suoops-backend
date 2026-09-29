"""Add index on invoice.issuer_id for faster filtering by business

Revision ID: 0015_add_index_invoice_issuer_id
Revises: 0014_add_invoice_issuer_fk
Create Date: 2025-11-07
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_add_index_invoice_issuer_id"
down_revision: Union[str, None] = "0014_add_invoice_issuer_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_invoice_issuer_id", "invoice", ["issuer_id"])


def downgrade() -> None:
    op.drop_index("ix_invoice_issuer_id", table_name="invoice")
