"""Fix users who have email stored as phone placeholder.

When users signed up with email, the email was stored in the phone column
as a required-field placeholder. Now that phone is nullable, we set those
back to NULL so the admin panel shows the correct state and phone
verification works properly.

Revision ID: fix_email_as_phone_20260304
Revises: remove_starter_plan_20260301
Create Date: 2026-03-04
"""

from alembic import op

# revision identifiers
revision = "fix_email_as_phone_20260304"
down_revision = "remove_starter_plan_20260301"
branch_labels = None
depends_on = None


def upgrade():
    # Set phone to NULL where phone looks like an email (contains @)
    # These users signed up with email and had their email stuffed into
    # the phone column as a placeholder.
    op.execute('UPDATE "user" SET phone = NULL, phone_verified = false ' "WHERE phone LIKE '%@%'")


def downgrade():
    # Restore email as phone placeholder for users with no phone
    op.execute('UPDATE "user" SET phone = email ' "WHERE phone IS NULL AND email IS NOT NULL")
