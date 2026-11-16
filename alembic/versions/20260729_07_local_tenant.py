"""provision the local API-token tenant

Revision ID: 20260729_07
Revises: 20260728_06
Create Date: 2026-07-29
"""

from alembic import op


revision = "20260729_07"
down_revision = "20260728_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tenants (id, name)
        VALUES ('compatibility', 'Local Procurement Workspace')
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    pass
