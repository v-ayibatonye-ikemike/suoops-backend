"""add prepaid wallet balance (kobo) and migrate invoice credits

Revision ID: 20260706_wallet_balance_kobo
Revises: 20260705_paystack_subaccount
Create Date: 2026-07-06

Introduces the commission billing model. Manual invoices are charged a fee
(max(3% of amount, ₦20)) from a prepaid wallet at creation, replacing the old
count-based invoice_balance. Existing invoice credits (bought at ₦25 each) are
migrated into the wallet at a goodwill rate of ₦30/credit = 3000 kobo.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260706_wallet_balance_kobo"
down_revision = "20260705_paystack_subaccount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "wallet_balance_kobo",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Migrate existing invoice credits into the wallet at ₦30/credit (3000 kobo).
    op.execute(
        'UPDATE "user" '
        "SET wallet_balance_kobo = COALESCE(invoice_balance, 0) * 3000 "
        "WHERE COALESCE(invoice_balance, 0) > 0"
    )


def downgrade() -> None:
    op.drop_column("user", "wallet_balance_kobo")
