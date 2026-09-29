"""Persist the SuoOps commission per invoice (fee ledger)

Revision ID: 20260721_invoice_fee_ledger
Revises: 20260720_audit_append_only
Create Date: 2026-07-21

Adds ``invoice.platform_fee_kobo`` — the commission (in kobo) actually locked in
for each invoice at creation. Commission reports previously RE-summed the fee by
applying the *current* rate to historical invoice amounts, so a rate change (the
manual-invoice cut from 3% to 1%) silently rewrote past earnings. Storing the fee
at creation makes reports read what was really charged.

Backfill: every historical revenue invoice is stamped with the fee it was charged
under the previous flat model (3%, min ₦20, tiered ₦2,000-per-₦500,000 cap) —
which applied to BOTH manual and storefront invoices before this release.
Expenses carry no fee and stay NULL.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260721_invoice_fee_ledger"
down_revision = "20260720_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice",
        sa.Column("platform_fee_kobo", sa.BigInteger(), nullable=True),
    )

    bind = op.get_bind()
    # Backfill historical revenue invoices at the OLD flat rate (3%, min ₦20,
    # ₦2,000 cap per ₦500,000 band). amount is Naira, so amount*3 = 3% in kobo.
    # Only stamp invoices with a sane amount — junk/test rows with astronomically
    # large amounts (whose tiered cap can exceed the column range) are left NULL;
    # reports exclude them anyway.
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE invoice
               SET platform_fee_kobo = LEAST(
                       GREATEST(ROUND(amount * 3), 2000),
                       200000 * CEIL(amount / 500000.0)
                   )
             WHERE invoice_type = 'revenue'
               AND platform_fee_kobo IS NULL
               AND amount > 0
               AND amount <= 50000000
            """
        )
    elif bind.dialect.name == "sqlite":
        # SQLite lacks CEIL/GREATEST/LEAST; compute the tiered fee with CAST math.
        op.execute(
            """
            UPDATE invoice
               SET platform_fee_kobo = MIN(
                       MAX(CAST(ROUND(amount * 3) AS INTEGER), 2000),
                       200000 * (CAST((amount - 1) / 500000 AS INTEGER) + 1)
                   )
             WHERE invoice_type = 'revenue'
               AND platform_fee_kobo IS NULL
               AND amount > 0
               AND amount <= 50000000
            """
        )


def downgrade() -> None:
    op.drop_column("invoice", "platform_fee_kobo")
