"""ensure workspace role names are unique

Revision ID: 20261117_18
Revises: 20261116_17
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261117_18"
down_revision = "20261116_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_roles_tenant_name",
        "roles",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_roles_tenant_name", table_name="roles")
