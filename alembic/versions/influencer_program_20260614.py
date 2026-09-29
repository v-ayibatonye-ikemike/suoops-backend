"""Add influencer/affiliate fields to referral_code table.

Supports custom vanity slugs, per-code commission rates,
bonus invoices for signups, and admin notes.

Revision ID: influencer_program_20260614
Revises: 20260612_admin_ip_allowlist
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "influencer_program_20260614"
down_revision: Union[str, None] = "20260612_admin_ip_allowlist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("referral_code", sa.Column("is_influencer", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("referral_code", sa.Column("custom_slug", sa.String(50), nullable=True))
    op.add_column("referral_code", sa.Column("influencer_name", sa.String(100), nullable=True))
    op.add_column("referral_code", sa.Column("influencer_contact", sa.String(200), nullable=True))
    op.add_column("referral_code", sa.Column("commission_first", sa.Integer(), server_default="500", nullable=False))
    op.add_column(
        "referral_code", sa.Column("commission_recurring", sa.Integer(), server_default="200", nullable=False)
    )
    op.add_column("referral_code", sa.Column("commission_months", sa.Integer(), server_default="2", nullable=False))
    op.add_column(
        "referral_code", sa.Column("commission_perpetual_pct", sa.Integer(), server_default="5", nullable=False)
    )
    op.add_column("referral_code", sa.Column("bonus_invoices", sa.Integer(), server_default="3", nullable=False))
    op.add_column("referral_code", sa.Column("notes", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_referral_code_custom_slug", "referral_code", ["custom_slug"])
    op.create_index("ix_referral_code_custom_slug", "referral_code", ["custom_slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_referral_code_custom_slug", table_name="referral_code")
    op.drop_constraint("uq_referral_code_custom_slug", "referral_code", type_="unique")
    op.drop_column("referral_code", "notes")
    op.drop_column("referral_code", "bonus_invoices")
    op.drop_column("referral_code", "commission_perpetual_pct")
    op.drop_column("referral_code", "commission_months")
    op.drop_column("referral_code", "commission_recurring")
    op.drop_column("referral_code", "commission_first")
    op.drop_column("referral_code", "influencer_contact")
    op.drop_column("referral_code", "influencer_name")
    op.drop_column("referral_code", "custom_slug")
    op.drop_column("referral_code", "is_influencer")
