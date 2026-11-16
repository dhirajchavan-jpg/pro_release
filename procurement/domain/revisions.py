"""Immutable tender and bid revision transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from procurement.domain.models import Bid, TenderVersion, new_id


def supersede_tender_version(current: TenderVersion, understanding: dict[str, Any]) -> TenderVersion:
    current.status = "superseded"
    current.superseded_at = datetime.now(timezone.utc)
    return TenderVersion(
        id=new_id(),
        tender_id=current.tender_id,
        previous_version_id=current.id,
        version_number=int(current.version_number) + 1,
        status="draft",
        understanding_json=understanding,
    )


def supersede_bid(current: Bid, *, snapshot: dict[str, Any]) -> Bid:
    current.status = "superseded"
    current.superseded_at = datetime.now(timezone.utc)
    current.revision_json = snapshot
    return Bid(
        id=new_id(),
        tenant_id=current.tenant_id,
        tender_version_id=current.tender_version_id,
        previous_bid_id=current.id,
        revision_number=int(current.revision_number or 1) + 1,
        bidder_name=current.bidder_name,
        status="uploaded",
    )
