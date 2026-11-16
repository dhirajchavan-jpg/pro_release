"""persist local operator profile

Revision ID: 20260923_15
Revises: 20260831_14
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260923_15"
down_revision = "20260831_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone")
    op.drop_column("users", "display_name")
