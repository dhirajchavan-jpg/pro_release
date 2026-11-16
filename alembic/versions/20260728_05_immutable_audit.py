"""tamper-evident immutable audit events

Revision ID: 20260728_05
Revises: 20260728_04
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_05"
down_revision = "20260728_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("previous_hash", sa.String(64), nullable=True))
    op.add_column("audit_events", sa.Column("event_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_audit_events_event_hash", "audit_events", ["event_hash"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_event_mutation()")
    op.drop_constraint("uq_audit_events_event_hash", "audit_events", type_="unique")
    op.drop_column("audit_events", "event_hash")
    op.drop_column("audit_events", "previous_hash")
