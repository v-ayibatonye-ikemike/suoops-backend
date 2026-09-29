"""add alert_events table

Revision ID: 20251107_add_alert_events
Revises: 20251107_add_monthly_tax_report
Create Date: 2025-11-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20251107_add_alert_events"
down_revision = "20251107_add_monthly_tax_report"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _table_exists("alert_events"):
        op.create_table(
            "alert_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("category", sa.String(50), nullable=False, index=True),
            sa.Column("severity", sa.String(20), nullable=False, server_default="error"),
            sa.Column("message", sa.String(500), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_alert_events_category_created", "alert_events", ["category", "created_at"], unique=False)


def downgrade() -> None:
    if _table_exists("alert_events"):
        op.drop_index("ix_alert_events_category_created", table_name="alert_events")
        op.drop_table("alert_events")


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()
