"""Evaluation queue routes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from procurement.api import _actor_user_id, _sessions
from procurement.domain.models import Evaluation, TenderVersion, WorkbenchBid, new_id
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import audit, enqueue
from procurement.workbench.common import _project, _state

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


class EvaluationSelection(BaseModel):
    vendor_id: str | None = Field(default=None, min_length=1, max_length=120)


@router.post("/projects/{project_id}/evaluate", status_code=status.HTTP_202_ACCEPTED)
def evaluate_project(
    project_id: str,
    request: Request,
    body: EvaluationSelection | None = None,
    force: bool = False,
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version = session.get(TenderVersion, project.tender_version_id)
        understanding = dict(version.understanding_json or {}) if version else {}
        if not version or version.status != "approved" or not (understanding.get("frozen_contract") or {}).get("criteria"):
            raise HTTPException(status_code=409, detail="finalize the evaluation checklist before evaluation")
        bidders = list(
            session.scalars(
                select(WorkbenchBid)
                .where(WorkbenchBid.project_id == project.id)
                .order_by(WorkbenchBid.created_at)
                .with_for_update()
            )
        )
        if not bidders:
            raise HTTPException(status_code=409, detail="at least one bid is required before evaluation")
        requested_vendor = body.vendor_id if body else None
        if requested_vendor:
            bidders = [row for row in bidders if row.vendor_key == requested_vendor]
            if not bidders:
                raise HTTPException(status_code=404, detail="bidder not found")
        pending = [row.vendor_key for row in bidders if row.classification_status != "approved"]
        bidders = [row for row in bidders if row.classification_status == "approved"]
        if requested_vendor and pending:
            raise HTTPException(status_code=409, detail=f"classification approval is required for: {requested_vendor}")
        if not bidders:
            raise HTTPException(status_code=409, detail="approve at least one bidder classification before evaluation")
        operations: list[dict[str, str]] = []
        for row in bidders:
            active = session.scalar(
                select(Evaluation)
                .where(
                    Evaluation.bid_id == row.bid_id,
                    Evaluation.status.in_({"queued", "running"}),
                )
                .order_by(Evaluation.created_at.desc())
                .with_for_update()
            )
            if active:
                continue
            existing = None
            if row.evaluation_id:
                existing = session.scalar(
                    select(Evaluation).where(Evaluation.id == row.evaluation_id).with_for_update()
                )
                if existing and (
                    existing.status in {"queued", "running"}
                    or (existing.status == "succeeded" and not force)
                ):
                    continue
            evaluation = Evaluation(id=new_id(), tenant_id=principal.tenant_id, tender_version_id=project.tender_version_id, bid_id=row.bid_id, status="queued")
            session.add(evaluation)
            operation = enqueue(
                session,
                tenant_id=principal.tenant_id,
                entity_type="evaluation",
                entity_id=evaluation.id,
                stage="evaluation",
                payload={
                    "evaluation_id": evaluation.id,
                    "tender_version_id": project.tender_version_id,
                    "bid_id": row.bid_id,
                    "previous_evaluation_id": row.evaluation_id,
                },
                key=None,
                request_hash=evaluation.id,
            )
            row.evaluation_id = evaluation.id
            operations.append({"vendor_id": row.vendor_key, "evaluation_id": evaluation.id, "operation_id": operation.id})
        project.updated_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="workbench.evaluation.queued", entity_type="workbench_project", entity_id=project.id, reason=None, before=None, after={"operations": operations})
        return {"operations": operations, "skipped_vendors": pending, "state": _state(session, project)}
