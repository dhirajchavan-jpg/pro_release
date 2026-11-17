"""remove superseded session and API-key authentication storage

Revision ID: 20261117_19
Revises: 20261117_18
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261117_19"
down_revision = "20261117_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE slug = 'keys:manage')"
    ))
    bind.execute(sa.text("DELETE FROM permissions WHERE slug = 'keys:manage'"))
    op.drop_table("api_keys")
    op.drop_table("user_sessions")


def downgrade() -> None:
    raise RuntimeError("JWT-only authentication migration cannot restore deleted session or API-key records")
