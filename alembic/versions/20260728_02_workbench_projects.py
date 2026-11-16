"""workbench project relations

Revision ID: 20260728_02
Revises: 20260728_01
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_02"
down_revision = "20260728_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("tender_id", sa.String(36), sa.ForeignKey("tenders.id"), nullable=False, unique=True),
        sa.Column("tender_version_id", sa.String(36), sa.ForeignKey("tender_versions.id"), nullable=False, unique=True),
        sa.Column("project_key", sa.String(120), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "project_key", name="uq_workbench_projects_tenant_key"),
    )
    op.create_index("ix_workbench_projects_tenant_id", "workbench_projects", ["tenant_id"])
    op.create_table(
        "workbench_bids",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("workbench_projects.id"), nullable=False),
        sa.Column("bid_id", sa.String(36), sa.ForeignKey("bids.id"), nullable=False, unique=True),
        sa.Column("vendor_key", sa.String(120), nullable=False),
        sa.Column("vendor_name", sa.String(512), nullable=False),
        sa.Column("evaluation_id", sa.String(36), sa.ForeignKey("evaluations.id"), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("project_id", "vendor_key", name="uq_workbench_bids_project_vendor"),
    )
    op.create_index("ix_workbench_bids_project_id", "workbench_bids", ["project_id"])


def downgrade() -> None:
    op.drop_table("workbench_bids")
    op.drop_table("workbench_projects")
