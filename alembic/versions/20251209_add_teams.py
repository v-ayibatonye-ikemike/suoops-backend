"""Add team management tables

Revision ID: 20251209_add_teams
Revises: 20251209_inventory_integration
Create Date: 2024-12-09

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251209_add_teams"
down_revision = "20251209_inventory_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create team table
    op.create_table(
        "team",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("max_members", sa.Integer(), server_default="3", nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("admin_user_id"),
    )
    op.create_index("ix_team_admin_user_id", "team", ["admin_user_id"])

    # Create team_member table
    op.create_table(
        "team_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Enum("admin", "member", name="teamrole"), server_default="member", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["team.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_member_team_user"),
    )
    op.create_index("ix_team_member_team_id", "team_member", ["team_id"])
    op.create_index("ix_team_member_user_id", "team_member", ["user_id"])

    # Create team_invitation table
    op.create_table(
        "team_invitation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "accepted", "expired", "revoked", name="invitationstatus"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["team.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "email", name="uq_team_invitation_team_email"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_team_invitation_team_id", "team_invitation", ["team_id"])
    op.create_index("ix_team_invitation_email", "team_invitation", ["email"])
    op.create_index("ix_team_invitation_token", "team_invitation", ["token"])


def downgrade() -> None:
    op.drop_table("team_invitation")
    op.drop_table("team_member")
    op.drop_table("team")

    # Drop enums
    sa.Enum(name="teamrole").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="invitationstatus").drop(op.get_bind(), checkfirst=True)
