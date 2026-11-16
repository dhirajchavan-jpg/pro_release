"""store project-level award suggestion on the workbench project

Revision ID: 20260819_10
Revises: 20260801_09
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260819_10"
down_revision = "20260801_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workbench_projects ADD COLUMN IF NOT EXISTS award_json JSONB")


def downgrade() -> None:
    op.drop_column("workbench_projects", "award_json")
