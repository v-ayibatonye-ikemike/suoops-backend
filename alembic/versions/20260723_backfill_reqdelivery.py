"""Backfill storefront_order_escrow.requires_delivery for existing service orders

Revision ID: 20260723_backfill_reqdelivery
Revises: 20260723_escrow_reqdelivery
Create Date: 2026-07-23

The requires_delivery column was added with server_default 'true', so orders
placed BEFORE that migration were all flagged as physical — even service /
digital ones. That made their invoice panel show the shipping flow (mark sent,
courier, delivery photo) instead of the service flow.

This backfill flips requires_delivery -> false for any existing order whose
line items ALL reference a non-physical product (service/digital), matching the
checkout rule ``no_delivery = all(fulfilment_type != 'physical')``. Orders with
any physical item, or any ad-hoc line with no product link, are left untouched.

NOTE: revision id kept <=32 chars — alembic_version.version_num is varchar(32).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260723_backfill_reqdelivery"
down_revision = "20260723_escrow_reqdelivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE storefront_order_escrow
        SET requires_delivery = false
        WHERE requires_delivery = true
          AND EXISTS (
            SELECT 1 FROM invoiceline il
            WHERE il.invoice_id = storefront_order_escrow.invoice_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM invoiceline il
            LEFT JOIN product p ON p.id = il.product_id
            WHERE il.invoice_id = storefront_order_escrow.invoice_id
              AND (
                il.product_id IS NULL
                OR COALESCE(p.fulfilment_type, 'physical') = 'physical'
              )
          )
        """
    )


def downgrade() -> None:
    # One-way data backfill — nothing to reverse safely.
    pass
