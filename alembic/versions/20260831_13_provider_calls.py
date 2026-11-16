"""add durable provider usage and cost ledger

Revision ID: 20260831_13
Revises: 20260831_12
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260831_13"
down_revision = "20260831_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("workbench_projects.id")),
        sa.Column("tender_version_id", sa.String(36), sa.ForeignKey("tender_versions.id")),
        sa.Column("bid_id", sa.String(36), sa.ForeignKey("bids.id")),
        sa.Column("evaluation_id", sa.String(36), sa.ForeignKey("evaluations.id")),
        sa.Column("operation_attempt", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("task", sa.String(128), nullable=False),
        sa.Column("endpoint_host", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cache_status", sa.String(16), nullable=False, server_default="miss"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_name", sa.String(32)),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8)),
        sa.Column("estimated_cost_inr", sa.Numeric(18, 8)),
        sa.Column("pricing_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("response_fingerprint", sa.String(64)),
        sa.Column("error_category", sa.String(64)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for column in (
        "tenant_id", "operation_id", "project_id", "tender_version_id", "bid_id",
        "evaluation_id", "stage", "capability", "provider", "status",
        "request_fingerprint", "error_category",
    ):
        op.create_index(f"ix_provider_calls_{column}", "provider_calls", [column])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_provider_call_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'provider_calls are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER provider_calls_immutable
        BEFORE UPDATE OR DELETE ON provider_calls
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_call_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS provider_calls_immutable ON provider_calls")
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_call_mutation()")
    op.drop_table("provider_calls")
