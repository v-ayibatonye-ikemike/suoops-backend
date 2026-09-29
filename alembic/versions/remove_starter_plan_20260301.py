"""Remove STARTER plan - migrate all STARTER users to FREE.

STARTER plan is no longer needed since users get 5 free invoices
and can buy invoice packs without a plan change.
Frontend shows "Starter" as a UX-only display label.

Revision ID: remove_starter_plan
Revises: None (run after latest migration)
Create Date: 2026-03-01
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "remove_starter_plan_20260301"
down_revision = "invoice_currency_20260223"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Migrate all STARTER users to FREE plan.

    - Updates all users with plan = 'starter' to plan = 'free'
    - Preserves invoice_balance (users paid for those invoices)
    - Does NOT remove 'starter' from the PostgreSQL enum type (safe approach)
    """
    # Update all STARTER users to FREE
    # Cast plan to text to avoid PostgreSQL enum validation on the WHERE clause
    # PostgreSQL enum values are UPPERCASE (see 0007_fix_enum_case migration)
    op.execute(sa.text("UPDATE \"user\" SET plan = 'FREE' WHERE plan::text = 'STARTER'"))


def downgrade() -> None:
    """No downgrade needed - STARTER plan is removed by design."""
    pass
