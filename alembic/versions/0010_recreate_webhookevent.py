"""recreate webhookevent table for paystack

Revision ID: 0010_recreate_webhookevent
Revises: 0009_add_customer_email
Create Date: 2025-11-09

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_recreate_webhookevent"
down_revision = "20251109_add_paid_at_receipt"
branch_labels = None
depends_on = None


def upgrade():
    """Recreate webhookevent table for tracking Paystack webhooks."""
    op.create_table(
        "webhookevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("signature", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_id", name="uq_webhookevent_provider_external_id"),
    )
    op.create_index(op.f("ix_webhookevent_provider"), "webhookevent", ["provider"])


def downgrade():
    """Drop webhookevent table."""
    op.drop_index(op.f("ix_webhookevent_provider"), "webhookevent")
    op.drop_table("webhookevent")
