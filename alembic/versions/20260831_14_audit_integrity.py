"""enforce audit hash shape for all new events

Revision ID: 20260831_14
Revises: 20260831_13
Create Date: 2026-08-31
"""

from alembic import op


revision = "20260831_14"
down_revision = "20260831_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE audit_events
        ADD CONSTRAINT ck_audit_events_event_hash_shape
        CHECK (event_hash IS NOT NULL AND char_length(event_hash) = 64) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE audit_events
        ADD CONSTRAINT ck_audit_events_previous_hash_shape
        CHECK (previous_hash IS NULL OR char_length(previous_hash) = 64) NOT VALID
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM audit_events
            WHERE event_hash IS NULL OR char_length(event_hash) <> 64
               OR (previous_hash IS NOT NULL AND char_length(previous_hash) <> 64)
          ) THEN
            ALTER TABLE audit_events VALIDATE CONSTRAINT ck_audit_events_event_hash_shape;
            ALTER TABLE audit_events VALIDATE CONSTRAINT ck_audit_events_previous_hash_shape;
          END IF;
        END
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_audit_events_previous_hash_shape", "audit_events", type_="check")
    op.drop_constraint("ck_audit_events_event_hash_shape", "audit_events", type_="check")
