"""add paystack subaccount columns to user

Revision ID: 20260705_paystack_subaccount
Revises: email_suppression_20260627
Create Date: 2026-07-05

Adds per-business Paystack subaccount tracking so invoice/marketplace payments
can be settled directly to the business's bank via split payments, with the
platform retaining a commission.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260705_paystack_subaccount"
down_revision = "email_suppression_20260627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("paystack_subaccount_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column(
            "paystack_subaccount_active",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_user_paystack_subaccount_code",
        "user",
        ["paystack_subaccount_code"],
    )
    op.add_column(
        "user",
        sa.Column(
            "storefront_enabled",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.add_column(
        "user",
        sa.Column("storefront_slug", sa.String(length=60), nullable=True),
    )
    op.create_index(
        "ix_user_storefront_slug",
        "user",
        ["storefront_slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_user_storefront_slug", table_name="user")
    op.drop_column("user", "storefront_slug")
    op.drop_column("user", "storefront_enabled")
    op.drop_index("ix_user_paystack_subaccount_code", table_name="user")
    op.drop_column("user", "paystack_subaccount_active")
    op.drop_column("user", "paystack_subaccount_code")
