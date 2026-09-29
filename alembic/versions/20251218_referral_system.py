"""add referral system tables

Revision ID: 20251218_referral_system
Revises: 20251218_whatsapp_optin
Create Date: 2024-12-18

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251218_referral_system"
down_revision = "20251218_whatsapp_optin"
branch_labels = None
depends_on = None


def upgrade():
    # Create referral_code table
    op.create_table(
        "referral_code",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referral_code_user_id", "referral_code", ["user_id"], unique=True)
    op.create_index("ix_referral_code_code", "referral_code", ["code"], unique=True)

    # Create referral table
    op.create_table(
        "referral",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("referral_code_id", sa.Integer(), nullable=False),
        sa.Column("referrer_id", sa.Integer(), nullable=False),
        sa.Column("referred_id", sa.Integer(), nullable=False),
        sa.Column("referral_type", sa.Enum("FREE_SIGNUP", "PAID_SIGNUP", name="referraltype"), nullable=False),
        sa.Column(
            "status", sa.Enum("PENDING", "COMPLETED", "EXPIRED", "FRAUDULENT", name="referralstatus"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["referral_code_id"],
            ["referral_code.id"],
        ),
        sa.ForeignKeyConstraint(
            ["referrer_id"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["referred_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referral_referral_code_id", "referral", ["referral_code_id"], unique=False)
    op.create_index("ix_referral_referrer_id", "referral", ["referrer_id"], unique=False)
    op.create_index("ix_referral_referred_id", "referral", ["referred_id"], unique=True)

    # Create referral_reward table
    op.create_table(
        "referral_reward",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reward_type", sa.String(50), nullable=False),
        sa.Column("reward_description", sa.String(255), nullable=False),
        sa.Column("free_referrals_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_referrals_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Enum("PENDING", "APPLIED", "EXPIRED", name="rewardstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referral_reward_user_id", "referral_reward", ["user_id"], unique=False)


def downgrade():
    op.drop_index("ix_referral_reward_user_id", table_name="referral_reward")
    op.drop_table("referral_reward")

    op.drop_index("ix_referral_referred_id", table_name="referral")
    op.drop_index("ix_referral_referrer_id", table_name="referral")
    op.drop_index("ix_referral_referral_code_id", table_name="referral")
    op.drop_table("referral")

    op.drop_index("ix_referral_code_code", table_name="referral_code")
    op.drop_index("ix_referral_code_user_id", table_name="referral_code")
    op.drop_table("referral_code")

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS referraltype")
    op.execute("DROP TYPE IF EXISTS referralstatus")
    op.execute("DROP TYPE IF EXISTS rewardstatus")
