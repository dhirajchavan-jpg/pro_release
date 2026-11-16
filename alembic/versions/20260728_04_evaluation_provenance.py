"""persist internal evaluation provenance

Revision ID: 20260728_04
Revises: 20260728_03
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_04"
down_revision = "20260728_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("internal_result_json", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluations", "internal_result_json")
