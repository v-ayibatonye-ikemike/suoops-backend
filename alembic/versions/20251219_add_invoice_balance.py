"""Add invoice_balance column to user table.

NEW BILLING MODEL:
- Users purchase invoice packs (100 invoices = ₦2,500)
- invoice_balance tracks available invoices
- Decremented on revenue invoice creation
- Default 5 for new users (free starter pack)

Revision ID: 20251219_add_invoice_balance
Revises: 20251218_whatsapp_optin, 20251218_admin_users
Create Date: 2025-12-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20251219_add_invoice_balance"
down_revision: Union[str, Sequence[str], None] = ("20251218_whatsapp_optin", "20251218_admin_users")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add invoice_balance column with default 5 (free starter invoices)
    op.add_column("user", sa.Column("invoice_balance", sa.Integer(), nullable=False, server_default="5"))

    # Migrate existing users: give them invoices based on their current plan
    # - FREE users: 5 invoices (starter pack)
    # - STARTER: 100 invoices (1 pack equivalent)
    # - PRO: 100 invoices (included with subscription)
    # - BUSINESS: 100 invoices (included with subscription)
    # Note: plan is an enum type in PostgreSQL, so we cast to text for comparison
    op.execute("""
        UPDATE "user" SET invoice_balance = CASE
            WHEN plan::text = 'free' THEN 5
            WHEN plan::text = 'starter' THEN 100
            WHEN plan::text = 'pro' THEN 100
            WHEN plan::text = 'business' THEN 100
            ELSE 5
        END
    """)


def downgrade() -> None:
    op.drop_column("user", "invoice_balance")
