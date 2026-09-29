"""set wallet_balance_kobo default to the ₦60 starter wallet

Revision ID: 20260706_wallet_starter_default
Revises: 20260706_wallet_balance_kobo
Create Date: 2026-07-06

New signups start with a ₦60 (6000 kobo) starter wallet so they can try manual
invoicing before topping up. Only changes the column default for future rows —
existing balances are untouched.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260706_wallet_starter_default"
down_revision = "20260706_wallet_balance_kobo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("user", "wallet_balance_kobo", server_default="6000")


def downgrade() -> None:
    op.alter_column("user", "wallet_balance_kobo", server_default="0")
