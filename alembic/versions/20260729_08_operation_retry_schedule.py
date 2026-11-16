"""schedule bounded operation retries

Revision ID: 20260729_08
Revises: 20260729_07
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_08"
down_revision = "20260729_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operations",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_operations_status_available_at",
        "operations",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operations_status_available_at", table_name="operations")
    op.drop_column("operations", "available_at")
