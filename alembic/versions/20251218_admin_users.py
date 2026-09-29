"""Add admin_users and support_tickets tables

Revision ID: 20251218_admin_users
Revises: 20251218_referral_system
Create Date: 2024-12-18

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251218_admin_users"
down_revision = "20251218_referral_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create admin_users table
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_super_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("can_manage_tickets", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_view_users", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_view_analytics", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_invite_admins", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("invited_by_id", sa.Integer(), nullable=True),
        sa.Column("invite_token", sa.String(255), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)
    op.create_index("ix_admin_users_invite_token", "admin_users", ["invite_token"], unique=True)

    # Create support_tickets table
    # Note: Foreign keys to users table removed to avoid dependency issues
    # The ORM model handles relationships; these are just optional reference IDs
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "category",
            sa.Enum("general", "billing", "technical", "feature", "account", "other", name="ticketcategory"),
            nullable=False,
            server_default="general",
        ),
        sa.Column(
            "status",
            sa.Enum("open", "in_progress", "waiting", "resolved", "closed", name="ticketstatus"),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "priority",
            sa.Enum("low", "medium", "high", "urgent", name="ticketpriority"),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("internal_notes", sa.Text(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_tickets_email", "support_tickets", ["email"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"])


def downgrade() -> None:
    # Drop support_tickets
    op.drop_index("ix_support_tickets_created_at", table_name="support_tickets")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_email", table_name="support_tickets")
    op.drop_table("support_tickets")
    op.execute("DROP TYPE IF EXISTS ticketcategory")
    op.execute("DROP TYPE IF EXISTS ticketstatus")
    op.execute("DROP TYPE IF EXISTS ticketpriority")

    # Drop admin_users
    op.drop_index("ix_admin_users_invite_token", table_name="admin_users")
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
