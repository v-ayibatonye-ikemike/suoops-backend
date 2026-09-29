"""add admin_login_audit table

Revision ID: 20260612_admin_login_audit
Revises: 20260427_signup_source
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "20260612_admin_login_audit"
down_revision = "20260427_signup_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_login_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("event", sa.String(40), nullable=False, server_default="login"),
        sa.Column("reason", sa.String(120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_admin_login_audit_admin_id", "admin_login_audit", ["admin_id"])
    op.create_index("ix_admin_login_audit_email", "admin_login_audit", ["email"])
    op.create_index("ix_admin_login_audit_ip", "admin_login_audit", ["ip"])
    op.create_index("ix_admin_login_audit_created_at", "admin_login_audit", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_login_audit_created_at", table_name="admin_login_audit")
    op.drop_index("ix_admin_login_audit_ip", table_name="admin_login_audit")
    op.drop_index("ix_admin_login_audit_email", table_name="admin_login_audit")
    op.drop_index("ix_admin_login_audit_admin_id", table_name="admin_login_audit")
    op.drop_table("admin_login_audit")
