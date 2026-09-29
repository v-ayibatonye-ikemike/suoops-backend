"""merge multiple heads

Revision ID: 20251110_02_merge
Revises: 20251110_01, 0010_recreate_webhookevent
Create Date: 2025-11-10 10:10:00.000000

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20251110_02_merge"
down_revision: Union[str, None] = ("20251110_01", "0010_recreate_webhookevent")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge migration - no schema changes
    pass


def downgrade() -> None:
    # Merge migration - no schema changes
    pass
