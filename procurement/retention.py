"""Delete expired document objects and their relational page records."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from procurement.domain.models import Document, DocumentPage
from procurement.platform.database import make_session_factory, session_scope
from procurement.platform.object_store import delete_file
from procurement.platform.operations import audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Delete expired objects; default is a dry run.")
    args = parser.parse_args()
    days = max(0, int(os.getenv("RETENTION_DAYS", "0")))
    if not days:
        print("Retention disabled: uploaded data is kept indefinitely.")
        return
    batch_size = max(1, int(os.getenv("RETENTION_BATCH_SIZE", "100")))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sessions = make_session_factory()
    object_keys: list[str] = []
    with session_scope(sessions) as session:
        documents = list(
            session.scalars(
                select(Document)
                .where(Document.created_at < cutoff)
                .order_by(Document.created_at)
                .limit(batch_size)
            )
        )
        if not args.apply:
            print(f"Retention dry run: {len(documents)} document(s) eligible before {cutoff.date()}.")
            return
        deleted = 0
        for document in documents:
            object_keys.append(document.object_key)
            session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
            audit(
                session,
                tenant_id=document.tenant_id,
                actor_user_id=None,
                event_type="document.retained.deleted",
                entity_type="document",
                entity_id=document.id,
                reason=f"RETENTION_DAYS={days}",
                before={"sha256": document.sha256, "created_at": document.created_at},
                after=None,
            )
            session.delete(document)
            deleted += 1
    for object_key in object_keys:
        delete_file(object_key)
    print(f"Retention applied: {deleted} document(s) deleted.")


if __name__ == "__main__":
    main()
