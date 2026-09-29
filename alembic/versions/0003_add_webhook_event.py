"""add webhook event table

Revision ID: 0003_add_webhook_event
Revises: 0002_add_invoice_discount
Create Date: 2025-10-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_add_webhook_event"
down_revision = "0002_add_invoice_discount"
branch_labels = None
depends_on = None


def upgrade() -> None:  # noqa: D401
    op.create_table(
        "webhookevent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False, index=True),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("signature", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_webhookevent_provider_external_id",
        "webhookevent",
        ["provider", "external_id"],
    )


def downgrade() -> None:  # noqa: D401
    op.drop_constraint("uq_webhookevent_provider_external_id", "webhookevent", type_="unique")
    op.drop_table("webhookevent")
