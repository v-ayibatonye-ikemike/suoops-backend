"""Add oauth_tokens table for secure token storage

Revision ID: 0035_oauth_tokens
Revises: 0034_payment_transactions
Create Date: 2025-11-22 11:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_oauth_tokens"
down_revision: Union[str, None] = "0034_payment_transactions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create oauth_tokens table for encrypted OAuth refresh token storage."""
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("token_type", sa.String(length=50), nullable=False, server_default="bearer"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index("ix_oauth_tokens_id", "oauth_tokens", ["id"])
    op.create_index("ix_oauth_tokens_user_id", "oauth_tokens", ["user_id"])
    op.create_index("ix_oauth_tokens_provider", "oauth_tokens", ["provider"])
    op.create_index("ix_oauth_tokens_revoked", "oauth_tokens", ["revoked_at"])
    op.create_index("ix_oauth_tokens_expires", "oauth_tokens", ["expires_at"])

    # Unique constraint: one token set per user per provider
    op.create_index("ix_oauth_tokens_user_provider", "oauth_tokens", ["user_id", "provider"], unique=True)


def downgrade() -> None:
    """Drop oauth_tokens table."""
    op.drop_index("ix_oauth_tokens_user_provider", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_expires", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_revoked", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_provider", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_user_id", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_id", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
