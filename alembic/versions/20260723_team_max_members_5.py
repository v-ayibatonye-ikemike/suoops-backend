"""Raise team.max_members default from 3 to 5

Revision ID: 20260723_team_max_members_5
Revises: 20260723_backfill_reqdelivery
Create Date: 2026-07-23

Business accounts can now invite up to 5 team members (was 3). This bumps the
column default and lifts existing teams still on the old default of 3 up to 5.
Teams with a custom higher cap (e.g. internal staff teams) are left untouched.

NOTE: revision id kept <=32 chars — alembic_version.version_num is varchar(32).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260723_team_max_members_5"
down_revision = "20260723_backfill_reqdelivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("team", "max_members", existing_type=sa.Integer(), server_default="5")
    op.execute("UPDATE team SET max_members = 5 WHERE max_members = 3")


def downgrade() -> None:
    op.alter_column("team", "max_members", existing_type=sa.Integer(), server_default="3")
    op.execute("UPDATE team SET max_members = 3 WHERE max_members = 5")
