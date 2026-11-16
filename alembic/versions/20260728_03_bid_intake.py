"""durable bidder intake

Revision ID: 20260728_03
Revises: 20260728_02
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_03"
down_revision = "20260728_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workbench_bids",
        sa.Column("classification_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "workbench_bids",
        sa.Column(
            "intake_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workbench_bids", "intake_json")
    op.drop_column("workbench_bids", "classification_status")
