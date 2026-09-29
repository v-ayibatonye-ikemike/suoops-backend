"""Add currency column to invoice table

Stores the invoice denomination currency (NGN or USD).
All existing invoices default to NGN.

Revision ID: invoice_currency_20260223
Revises: user_currency_pref_20260223
Create Date: 2026-02-23
"""

import sqlalchemy as sa

from alembic import op

revision = "invoice_currency_20260223"
down_revision = "user_currency_pref_20260223"


def upgrade():
    op.add_column(
        "invoice",
        sa.Column(
            "currency",
            sa.String(3),
            server_default="NGN",
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column("invoice", "currency")
