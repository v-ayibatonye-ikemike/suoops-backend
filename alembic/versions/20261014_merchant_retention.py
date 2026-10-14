"""Merchant lifecycle and bounded retention pilot.

Revision ID: 20261014_merchant_retention
Revises: 20261003_ai_governance
"""

import sqlalchemy as sa

from alembic import op

revision = "20261014_merchant_retention"
down_revision = "20261003_ai_governance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "merchant_lifecycle",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("reactivated_at", sa.DateTime(timezone=True)),
        sa.Column("whatsapp_opted_in", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("consent_source", sa.String(30)),
        sa.Column("consented_phone", sa.String(32)),
    )
    op.create_table(
        "retention_experiment",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id"), nullable=False),
    )
    op.create_table(
        "retention_enrollment",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("experiment_id", sa.Integer(), sa.ForeignKey("retention_experiment.id"), nullable=False),
        sa.Column("arm", sa.String(10), nullable=False),
        sa.Column("initial_state", sa.String(20), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
    )
    op.create_index("ix_retention_enrollment_experiment_id", "retention_enrollment", ["experiment_id"])
    op.create_table(
        "retention_contact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("experiment_id", sa.Integer(), sa.ForeignKey("retention_experiment.id"), nullable=False),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(120), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message_id", sa.String(255), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(300)),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_retention_contact"),
    )
    for column in ("user_id", "experiment_id", "created_at"):
        op.create_index(f"ix_retention_contact_{column}", "retention_contact", [column])


def downgrade():
    for table in ("retention_contact", "retention_enrollment", "retention_experiment", "merchant_lifecycle"):
        op.drop_table(table)
