"""add JWT token version for revocation

Revision ID: 20261117_20
Revises: 20261117_19
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261117_20"
down_revision = "20261117_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.alter_column("users", "token_version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "token_version")
