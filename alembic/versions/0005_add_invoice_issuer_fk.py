"""Add foreign key constraint for invoice.issuer_id -> user.id

Revision ID: 0014_add_invoice_issuer_fk
Revises: 0013_add_tax_fiscalization
Create Date: 2025-11-07
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_add_invoice_issuer_fk"
down_revision: Union[str, None] = "0013_add_tax_fiscalization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add foreign key constraint; assuming existing data has valid issuer_id references.
    with op.batch_alter_table("invoice") as batch_op:
        batch_op.create_foreign_key("fk_invoice_issuer_id_user", "user", ["issuer_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch_op:
        batch_op.drop_constraint("fk_invoice_issuer_id_user", type_="foreignkey")
