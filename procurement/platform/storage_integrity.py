"""Read-only database/object-store consistency verification."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from procurement.domain.models import Bid, Document, TenderVersion, WorkbenchBid
from procurement.platform.object_store import list_keys


def _embedded_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for name, child in value.items():
            if name == "object_key" and isinstance(child, str) and child:
                found.add(child)
            elif name == "object_keys" and isinstance(child, list):
                found.update(str(item) for item in child if str(item))
            found.update(_embedded_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_embedded_keys(child))
    return found


def referenced_object_keys(session: Any) -> set[str]:
    keys = set(session.scalars(select(Document.object_key)))
    for model, column in (
        (TenderVersion, TenderVersion.understanding_json),
        (WorkbenchBid, WorkbenchBid.intake_json),
        (Bid, Bid.revision_json),
    ):
        for value in session.scalars(select(column).select_from(model)):
            keys.update(_embedded_keys(value))
    return {str(key) for key in keys if str(key)}


def verify_storage_consistency(session: Any) -> dict[str, Any]:
    prefix = os.getenv("OBJECT_STORAGE_PREFIX", "procurement").strip().strip("/")
    referenced = referenced_object_keys(session)
    stored = list_keys(prefix)
    missing = sorted(referenced - stored)
    unreferenced = sorted(stored - referenced)
    return {
        "valid": not missing and not unreferenced,
        "referenced_count": len(referenced),
        "stored_count": len(stored),
        "missing": missing,
        "unreferenced": unreferenced,
    }
