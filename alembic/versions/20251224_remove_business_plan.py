"""Remove BUSINESS plan - focus on small/medium businesses only

Revision ID: 20251224_remove_business
Revises: 20251221_add_cit
Create Date: 2024-12-24

Changes:
- Downgrade any BUSINESS plan users to PRO
- BUSINESS plan features (voice, OCR, API) now available on PRO
- Target market: businesses under ₦100M annual revenue
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251224_remove_business"
down_revision = "20251221_add_cit"
branch_labels = None
depends_on = None


def upgrade():
    # Step 1: Update any users on BUSINESS plan to PRO
    # PostgreSQL enum uses UPPERCASE values (see 0007_fix_enum_case.py)
    # This is safe even if no users are on BUSINESS - UPDATE affects 0 rows
    op.execute("""
        UPDATE "user"
        SET plan = 'PRO'
        WHERE plan = 'BUSINESS'
    """)

    # Also handle ENTERPRISE users if any exist (migrating to PRO)
    op.execute("""
        UPDATE "user"
        SET plan = 'PRO'
        WHERE plan = 'ENTERPRISE'
    """)

    # Note: We don't remove the enum values from PostgreSQL
    # because it requires recreating the column which is risky
    # The application code will simply not use BUSINESS/ENTERPRISE anymore


def downgrade():
    # No downgrade - users stay on PRO which is equivalent or better
    pass
