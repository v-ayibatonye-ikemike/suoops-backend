"""Add missing indexes for scalability.

Covers the most expensive query patterns:
- user.last_login (admin stats, inactive checks)
- user(plan, subscription_expires_at) (subscription expiry task)
- invoice(issuer_id, created_at) (dashboard date-range queries)
- invoice(issuer_id, invoice_type) (quota checks, aggregations)
- user_email_log(user_id, email_type) (dedup checks in all tasks)

Revision ID: scalability_indexes_20260614
Revises: influencer_program_20260614
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "scalability_indexes_20260614"
down_revision: Union[str, None] = "influencer_program_20260614"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # User indexes for admin stats and scheduled tasks
    op.create_index(
        "ix_user_last_login",
        "user",
        ["last_login"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_user_plan_sub_expires",
        "user",
        ["plan", "subscription_expires_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_user_created_at",
        "user",
        ["created_at"],
        if_not_exists=True,
    )

    # Invoice composite indexes for heavy query patterns
    op.create_index(
        "ix_invoice_issuer_created",
        "invoice",
        ["issuer_id", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_invoice_issuer_type",
        "invoice",
        ["issuer_id", "invoice_type"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_invoice_issuer_status",
        "invoice",
        ["issuer_id", "status"],
        if_not_exists=True,
    )

    # UserEmailLog composite index (dedup lookups happen in every task)
    op.create_index(
        "ix_email_log_user_type",
        "user_email_log",
        ["user_id", "email_type"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_email_log_user_type", table_name="user_email_log", if_exists=True)
    op.drop_index("ix_invoice_issuer_status", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_issuer_type", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_issuer_created", table_name="invoice", if_exists=True)
    op.drop_index("ix_user_created_at", table_name="user", if_exists=True)
    op.drop_index("ix_user_plan_sub_expires", table_name="user", if_exists=True)
    op.drop_index("ix_user_last_login", table_name="user", if_exists=True)
