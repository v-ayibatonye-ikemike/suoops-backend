"""storefront moderation + anti-fraud signup signals

Revision ID: 20260708_store_moderation_fraud
Revises: 20260707_storefront_upgrade
Create Date: 2026-07-08

Adds two related capabilities to the ``user`` table:

Storefront moderation (Trust & Safety):
  - ``store_status``         : active | suspended | delisted (indexed)
  - ``store_status_reason``  : free-text reason shown to admins
  - ``store_status_at``      : when the status last changed
  - ``store_status_by_id``   : AdminUser.id that made the change

Anti-fraud / duplicate-account signals captured at signup:
  - ``signup_ip``            : originating IP (indexed for velocity checks)
  - ``signup_device_id``     : client device fingerprint (indexed for linking)
  - ``signup_user_agent``    : raw UA string
  - ``risk_score``           : heuristic 0-100 score
  - ``risk_signals``         : JSON list of triggered signal codes
  - ``flagged_for_review``   : needs manual review (indexed)
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260708_store_moderation_fraud"
down_revision = "20260707_storefront_upgrade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Storefront moderation ──
    op.add_column(
        "user",
        sa.Column(
            "store_status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column("user", sa.Column("store_status_reason", sa.String(length=255), nullable=True))
    op.add_column("user", sa.Column("store_status_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user", sa.Column("store_status_by_id", sa.Integer(), nullable=True))
    op.create_index("ix_user_store_status", "user", ["store_status"])

    # ── Anti-fraud / duplicate-account signals ──
    op.add_column("user", sa.Column("signup_ip", sa.String(length=45), nullable=True))
    op.add_column("user", sa.Column("signup_device_id", sa.String(length=64), nullable=True))
    op.add_column("user", sa.Column("signup_user_agent", sa.String(length=400), nullable=True))
    op.add_column(
        "user",
        sa.Column("risk_score", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("user", sa.Column("risk_signals", sa.JSON(), nullable=True))
    op.add_column(
        "user",
        sa.Column(
            "flagged_for_review",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.create_index("ix_user_signup_ip", "user", ["signup_ip"])
    op.create_index("ix_user_signup_device_id", "user", ["signup_device_id"])
    op.create_index("ix_user_flagged_for_review", "user", ["flagged_for_review"])


def downgrade() -> None:
    op.drop_index("ix_user_flagged_for_review", table_name="user")
    op.drop_index("ix_user_signup_device_id", table_name="user")
    op.drop_index("ix_user_signup_ip", table_name="user")
    op.drop_column("user", "flagged_for_review")
    op.drop_column("user", "risk_signals")
    op.drop_column("user", "risk_score")
    op.drop_column("user", "signup_user_agent")
    op.drop_column("user", "signup_device_id")
    op.drop_column("user", "signup_ip")

    op.drop_index("ix_user_store_status", table_name="user")
    op.drop_column("user", "store_status_by_id")
    op.drop_column("user", "store_status_at")
    op.drop_column("user", "store_status_reason")
    op.drop_column("user", "store_status")
