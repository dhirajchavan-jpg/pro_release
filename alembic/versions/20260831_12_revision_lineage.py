"""add immutable tender and bid revision lineage

Revision ID: 20260831_12
Revises: 20260831_11
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260831_12"
down_revision = "20260831_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tender_versions", sa.Column("previous_version_id", sa.String(36), nullable=True))
    op.add_column("tender_versions", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_tender_versions_previous_version",
        "tender_versions",
        "tender_versions",
        ["previous_version_id"],
        ["id"],
    )
    op.create_index("ix_tender_versions_previous_version_id", "tender_versions", ["previous_version_id"])

    op.add_column("documents", sa.Column("tender_version_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_documents_tender_version",
        "documents",
        "tender_versions",
        ["tender_version_id"],
        ["id"],
    )
    op.create_index("ix_documents_tender_version_id", "documents", ["tender_version_id"])
    op.execute(
        """
        UPDATE documents AS document
        SET tender_version_id = project.tender_version_id
        FROM workbench_projects AS project
        WHERE document.tender_id = project.tender_id
          AND document.source_kind <> 'superseded_upload'
          AND document.tender_version_id IS NULL
        """
    )

    op.add_column("bids", sa.Column("previous_bid_id", sa.String(36), nullable=True))
    op.add_column("bids", sa.Column("revision_number", sa.Integer(), server_default="1", nullable=False))
    op.add_column(
        "bids",
        sa.Column(
            "revision_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("bids", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_bids_previous_bid", "bids", "bids", ["previous_bid_id"], ["id"])
    op.create_index("ix_bids_previous_bid_id", "bids", ["previous_bid_id"])
    op.alter_column("bids", "revision_number", server_default=None)
    op.alter_column("bids", "revision_json", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_bids_previous_bid_id", table_name="bids")
    op.drop_constraint("fk_bids_previous_bid", "bids", type_="foreignkey")
    op.drop_column("bids", "superseded_at")
    op.drop_column("bids", "revision_json")
    op.drop_column("bids", "revision_number")
    op.drop_column("bids", "previous_bid_id")

    op.drop_index("ix_documents_tender_version_id", table_name="documents")
    op.drop_constraint("fk_documents_tender_version", "documents", type_="foreignkey")
    op.drop_column("documents", "tender_version_id")

    op.drop_index("ix_tender_versions_previous_version_id", table_name="tender_versions")
    op.drop_constraint("fk_tender_versions_previous_version", "tender_versions", type_="foreignkey")
    op.drop_column("tender_versions", "superseded_at")
    op.drop_column("tender_versions", "previous_version_id")
