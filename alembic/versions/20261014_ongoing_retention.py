"""Ongoing retention channels and email preferences; preserve historical cohorts."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_ongoing_retention"
down_revision = "20261014_merchant_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "merchant_lifecycle",
        sa.Column("email_opted_out", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(sa.text(
        "UPDATE merchant_lifecycle SET email_opted_out = true "
        "WHERE consent_at IS NOT NULL AND whatsapp_opted_in = false"
    ))
    with op.batch_alter_table("retention_contact") as batch:
        batch.add_column(sa.Column("channel", sa.String(20), nullable=False, server_default="whatsapp"))
        batch.alter_column("experiment_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT id FROM retention_contact WHERE experiment_id IS NULL LIMIT 1"
    )).first():
        raise RuntimeError("Ongoing retention contacts exist; preserve their history instead of downgrading.")
    with op.batch_alter_table("retention_contact") as batch:
        batch.alter_column("experiment_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("channel")
    op.drop_column("merchant_lifecycle", "email_opted_out")
