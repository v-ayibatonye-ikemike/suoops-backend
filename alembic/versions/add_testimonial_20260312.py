"""Add testimonial table for user feedback and landing page showcase.

Revision ID: add_testimonial_20260312
Revises: fix_email_as_phone_20260304
Create Date: 2026-03-12
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "add_testimonial_20260312"
down_revision = "fix_email_as_phone_20260304"


def upgrade() -> None:
    op.create_table(
        "testimonial",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("rating", sa.Integer(), server_default="5", nullable=False),
        sa.Column("approved", sa.Boolean(), server_default="false", nullable=False, index=True),
        sa.Column("featured", sa.Boolean(), server_default="false", nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("testimonial")
