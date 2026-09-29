"""Add WhatsApp opt-in tracking fields.

Revision ID: 20251218_whatsapp_optin
Revises: fix_customer_phone_len
Create Date: 2025-12-18
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251218_whatsapp_optin"
down_revision = "fix_customer_phone_len"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add whatsapp_opted_in to customer table
    op.add_column(
        "customer",
        sa.Column("whatsapp_opted_in", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Add whatsapp_delivery_pending to invoice table
    op.add_column(
        "invoice",
        sa.Column("whatsapp_delivery_pending", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Add index for pending deliveries
    op.create_index(
        "ix_invoice_whatsapp_delivery_pending",
        "invoice",
        ["whatsapp_delivery_pending"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_whatsapp_delivery_pending", table_name="invoice")
    op.drop_column("invoice", "whatsapp_delivery_pending")
    op.drop_column("customer", "whatsapp_opted_in")
