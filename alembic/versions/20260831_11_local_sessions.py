"""enforce one active local browser session

Revision ID: 20260831_11
Revises: 20260819_10
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_11"
down_revision = "20260819_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_sessions",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_local_sessions_expires_at", "local_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("local_sessions")
