"""durable clarification lifecycle

Revision ID: 20260728_06
Revises: 20260728_05
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_06"
down_revision = "20260728_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clarifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("workbench_projects.id"), nullable=False),
        sa.Column("workbench_bid_id", sa.String(36), sa.ForeignKey("workbench_bids.id"), nullable=False),
        sa.Column("evaluation_id", sa.String(36), sa.ForeignKey("evaluations.id"), nullable=True),
        sa.Column("criterion_id", sa.String(255), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_clarifications_tenant_id", "clarifications", ["tenant_id"])
    op.create_index("ix_clarifications_project_id", "clarifications", ["project_id"])
    op.create_index("ix_clarifications_workbench_bid_id", "clarifications", ["workbench_bid_id"])
    op.create_index("ix_clarifications_evaluation_id", "clarifications", ["evaluation_id"])


def downgrade() -> None:
    op.drop_table("clarifications")
