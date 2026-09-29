"""add admin_ip_allowlist table

Revision ID: 20260612_admin_ip_allowlist
Revises: 20260612_admin_login_audit
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "20260612_admin_ip_allowlist"
down_revision = "20260612_admin_login_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_ip_allowlist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cidr", sa.String(64), nullable=False),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_admin_ip_allowlist_cidr", "admin_ip_allowlist", ["cidr"], unique=True)
    op.create_index("ix_admin_ip_allowlist_created_by_id", "admin_ip_allowlist", ["created_by_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_ip_allowlist_created_by_id", table_name="admin_ip_allowlist")
    op.drop_index("ix_admin_ip_allowlist_cidr", table_name="admin_ip_allowlist")
    op.drop_table("admin_ip_allowlist")
