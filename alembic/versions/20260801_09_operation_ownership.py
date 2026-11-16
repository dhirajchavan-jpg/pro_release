"""add fenced operation leases and progress versions

Revision ID: 20260801_09
Revises: 20260729_08
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260801_09"
down_revision = "20260729_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("state_version", sa.Integer(), server_default="0", nullable=False))
    op.add_column("operations", sa.Column("progress_version", sa.Integer(), server_default="0", nullable=False))
    op.add_column("operations", sa.Column("lease_id", sa.String(36), nullable=True))
    op.add_column("operations", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operations", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_operations_lease_id", "operations", ["lease_id"])
    op.create_check_constraint(
        "ck_operations_status_valid",
        "operations",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.create_index(
        "ix_operations_status_lease_expires_at",
        "operations",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operations_status_lease_expires_at", table_name="operations")
    op.drop_constraint("ck_operations_status_valid", "operations", type_="check")
    op.drop_constraint("uq_operations_lease_id", "operations", type_="unique")
    op.drop_column("operations", "heartbeat_at")
    op.drop_column("operations", "lease_expires_at")
    op.drop_column("operations", "lease_id")
    op.drop_column("operations", "progress_version")
    op.drop_column("operations", "state_version")
