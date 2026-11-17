"""add rotating JWT refresh-token registry

Revision ID: 20261117_21
Revises: 20261117_20
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261117_21"
down_revision = "20261117_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
