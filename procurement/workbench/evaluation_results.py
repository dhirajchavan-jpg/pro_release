"""Evaluation results, clarification, and human-review routes."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from procurement.api import _actor_user_id, _owned, _sessions
from procurement.domain.models import (
    AuditEvent,
    Bid,
    Clarification,
    Document,
    Evaluation,
    Review,
    TenderVersion,
    WorkbenchBid,
    WorkbenchProject,
    new_id,
)
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import audit
from procurement.pipelines.bid_evaluation import approved_requirement_matrix
from procurement.workbench.common import (
    ClarificationClose,
    ClarificationCreate,
    ClarificationResponse,
    HumanCriterionReview,
    _apply_human_reviews,
    _bidder,
    _normalize_public_report,
    _project,
    _state,
    _ui_evaluation,
    decision_summary,
)
from procurement.workbench.evaluation_awards import refresh_award_recommendation

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


_BHEL_SCHEMA_VERSION = "bhel-bid-evaluation.v1"
_BHEL_COMPARATIVE_SCHEMA_VERSION = "bhel-bid-comparative.v1"


def _unevaluated_bid_requirements(
    understanding: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        matrix, _ = approved_requirement_matrix(understanding)
    except ValueError:
        return []
    unevaluated = []
    for criterion in matrix.items:
        missing_for = []
        for vendor_id, result in results.items():
            if result.get("status") != "succeeded":
                continue
            present = {
                str(check.get("criterion_id") or group.get("criterion_id") or "")
                for group in result.get("criteria") or []
                for check in (group.get("checks") or [group])
            }
            if criterion.criterion_id not in present:
                missing_for.append(vendor_id)
        if missing_for:
            unevaluated.append(
                {
                    "criterion_id": criterion.criterion_id,
                    "title": criterion.requirement,
                    "vendor_ids": missing_for,
                }
            )
    return unevaluated


def _bhel_extension(internal_result_json: dict[str, Any] | None) -> dict[str, Any] | None:
    internal_report = (internal_result_json or {}).get("report") or {}
    if not isinstance(internal_report, dict):
        return None
    extensions = internal_report.get("extensions") or {}
    extension = extensions.get("bhel") if isinstance(extensions, dict) else None
    return deepcopy(extension) if isinstance(extension, dict) else None


def _report_bhel_extension(report: dict[str, Any]) -> dict[str, Any] | None:
    extensions = report.get("extensions") or {}
    extension = extensions.get("bhel") if isinstance(extensions, dict) else None
    return extension if isinstance(extension, dict) else None


def _attach_bhel_extension(
    report: dict[str, Any],
    internal_result_json: dict[str, Any] | None,
) -> dict[str, Any]:
    extension = _bhel_extension(internal_result_json)
    if extension is None:
        return report
    result = dict(report)
    extensions = result.get("extensions")
    extensions = dict(extensions) if isinstance(extensions, dict) else {}
    extensions["bhel"] = extension
    result["extensions"] = extensions
    return result


def _apply_bhel_reviews_to_report(session: Any, evaluation: Evaluation, report: dict[str, Any]) -> dict[str, Any]:
    extension = _report_bhel_extension(report)
    if extension is None:
        return report
    from procurement.bhel_evaluation import apply_bhel_human_reviews

    reviews = list(
        session.scalars(
            select(Review)
            .where(Review.evaluation_id == evaluation.id)
            .order_by(Review.created_at, Review.id)
        )
    )
    result = dict(report)
    extensions = dict(result.get("extensions") or {})
    extensions["bhel"] = apply_bhel_human_reviews(extension, reviews)
    result["extensions"] = extensions
    return result


def _reject_bhel_clarification() -> None:
    from procurement.bhel_evaluation import load_bhel_profile

    profile = load_bhel_profile()
    policy = profile.get("submission_policy") if isinstance(profile, dict) else {}
    if isinstance(policy, dict) and policy.get("clarifications_allowed") is False:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Clarifications are disabled for the active BHEL evaluation profile.",
                "code": "BHEL_CLARIFICATIONS_DISABLED",
            },
        )


def _bhel_comparative_result(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from procurement.bhel_evaluation import bhel_comparative_summary

    result = bhel_comparative_summary(reports)
    if not isinstance(result, dict) or result.get("schema_version") != _BHEL_COMPARATIVE_SCHEMA_VERSION:
        raise HTTPException(
            status_code=500,
            detail={"message": "BHEL comparative result is invalid.", "code": "BHEL_COMPARATIVE_INVALID"},
        )
    return result


@router.get("/projects/{project_id}/results")
def project_results(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        bidders = list(
            session.scalars(
                select(WorkbenchBid)
                .where(WorkbenchBid.project_id == project.id)
                .order_by(WorkbenchBid.created_at)
            )
        )
        version = session.get(TenderVersion, project.tender_version_id)
        understanding = dict(version.understanding_json or {}) if version else {}
        results: dict[str, Any] = {}
        complete = bool(bidders)
        evaluated = 0
        outcomes = {"pass": 0, "fail": 0, "review": 0}
        for row in bidders:
            evaluation = session.get(Evaluation, row.evaluation_id) if row.evaluation_id else None
            results[row.vendor_key] = _ui_evaluation(
                evaluation,
                project_id=project.id,
                vendor_key=row.vendor_key,
                intake=dict(row.intake_json or {}),
                understanding=understanding,
            )
            if evaluation:
                _apply_human_reviews(session, evaluation, results[row.vendor_key])
            results[row.vendor_key]["decision_summary"] = decision_summary(
                results[row.vendor_key].get("criteria") or [],
                results[row.vendor_key].get("final_decision"),
                (evaluation.result_json or {}).get("qualifies") if evaluation else None,
            )
            results[row.vendor_key]["vendor_name"] = row.vendor_name
            if evaluation and evaluation.status == "succeeded":
                public_report = evaluation.result_json
                results[row.vendor_key]["report"] = (
                    _apply_bhel_reviews_to_report(
                        session,
                        evaluation,
                        _normalize_public_report(
                            _attach_bhel_extension(dict(public_report), evaluation.internal_result_json)
                        ),
                    )
                    if isinstance(public_report, dict)
                    else public_report
                )
            else:
                results[row.vendor_key]["report"] = None
            complete = complete and bool(evaluation and evaluation.status == "succeeded")
            if evaluation and evaluation.status == "succeeded":
                evaluated += 1
                outcome = str(results[row.vendor_key]["decision_summary"].get("outcome") or "review")
                outcomes[outcome if outcome in outcomes else "review"] += 1
        unevaluated = _unevaluated_bid_requirements(understanding, results)
        return {
            "state": _state(session, project),
            "complete": complete,
            "comparison": {
                "bidder_count": len(bidders),
                "evaluated_count": evaluated,
                "pending_count": len(bidders) - evaluated,
                "outcomes": outcomes,
            },
            "unevaluated_requirements": unevaluated,
            "vendors": results,
        }


review_router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


def _clarification_payload(row: Clarification, bidder: WorkbenchBid | None = None) -> dict[str, Any]:
    return {
        "clarification_id": row.id,
        "vendor_key": bidder.vendor_key if bidder else None,
        "criterion_id": row.criterion_id,
        "question": row.question_text,
        "response": row.response_text,
        "status": row.status,
        "issued_at": row.issued_at,
        "responded_at": row.responded_at,
        "closed_at": row.closed_at,
        "created_at": row.created_at,
    }


@review_router.get("/projects/{project_id}/clarifications")
def list_clarifications(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        bidders = {
            row.id: row
            for row in session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id))
        }
        rows = list(
            session.scalars(
                select(Clarification)
                .where(Clarification.project_id == project.id, Clarification.tenant_id == principal.tenant_id)
                .order_by(Clarification.created_at)
            )
        )
        return {"clarifications": [_clarification_payload(row, bidders.get(row.workbench_bid_id)) for row in rows]}


@review_router.post("/projects/{project_id}/clarifications", status_code=status.HTTP_201_CREATED)
def create_clarification(project_id: str, body: ClarificationCreate, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    _reject_bhel_clarification()
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        bidder = _bidder(session, project, body.vendor_key)
        row = Clarification(
            id=new_id(),
            tenant_id=principal.tenant_id,
            project_id=project.id,
            workbench_bid_id=bidder.id,
            evaluation_id=bidder.evaluation_id,
            criterion_id=body.criterion_id,
            question_text=body.question,
            status="draft",
            created_by_user_id=actor_id,
        )
        session.add(row)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="clarification.created", entity_type="clarification", entity_id=row.id, reason=body.question, before=None, after={"status": "draft"})
        return _clarification_payload(row, bidder)


@review_router.post("/clarifications/{clarification_id}/issue")
def issue_clarification(clarification_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver")
    _reject_bhel_clarification()
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        row = _owned(session, Clarification, clarification_id, principal.tenant_id)
        if row.status != "draft":
            raise HTTPException(status_code=409, detail="only draft clarifications can be issued")
        row.status = "issued"
        row.issued_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="clarification.issued", entity_type="clarification", entity_id=row.id, reason=None, before={"status": "draft"}, after={"status": "issued"})
        return _clarification_payload(row, session.get(WorkbenchBid, row.workbench_bid_id))


@review_router.post("/clarifications/{clarification_id}/response")
def record_clarification_response(clarification_id: str, body: ClarificationResponse, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    _reject_bhel_clarification()
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        row = _owned(session, Clarification, clarification_id, principal.tenant_id)
        if row.status != "issued":
            raise HTTPException(status_code=409, detail="only issued clarifications can receive a response")
        row.response_text = body.response
        row.status = "responded"
        row.responded_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="clarification.responded", entity_type="clarification", entity_id=row.id, reason=None, before={"status": "issued"}, after={"status": "responded"})
        return _clarification_payload(row, session.get(WorkbenchBid, row.workbench_bid_id))


@review_router.post("/clarifications/{clarification_id}/close")
def close_clarification(clarification_id: str, body: ClarificationClose, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver")
    _reject_bhel_clarification()
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        row = _owned(session, Clarification, clarification_id, principal.tenant_id)
        if row.status != "responded":
            raise HTTPException(status_code=409, detail="only responded clarifications can be closed")
        row.status = "closed"
        row.closed_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="clarification.closed", entity_type="clarification", entity_id=row.id, reason=body.reason, before={"status": "responded"}, after={"status": "closed"})
        return _clarification_payload(row, session.get(WorkbenchBid, row.workbench_bid_id))


@review_router.post("/projects/{project_id}/vendors/{vendor_key}/criteria/{criterion_id}/human-review")
def review_criterion(
    project_id: str,
    vendor_key: str,
    criterion_id: str,
    body: HumanCriterionReview,
    request: Request,
) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        row = _bidder(session, project, vendor_key)
        evaluation = session.get(Evaluation, row.evaluation_id) if row.evaluation_id else None
        if not evaluation or evaluation.status != "succeeded":
            raise HTTPException(status_code=409, detail="evaluation is not complete")
        internal = dict(evaluation.internal_result_json or {}).get("report") or {}
        known = {item.get("criterion_id") for item in internal.get("findings") or []}
        extension = _bhel_extension(evaluation.internal_result_json) or {}
        for check in extension.get("checks") or []:
            if isinstance(check, dict):
                known.add(check.get("criterion_id") or check.get("check_id"))
        normalized_known = {
            str(value).removeprefix("BHEL-").casefold()
            for value in known
            if value not in (None, "")
        }
        if criterion_id.removeprefix("BHEL-").casefold() not in normalized_known:
            raise HTTPException(status_code=404, detail="evaluation criterion not found")
        payload = body.model_dump()
        payload["criterion_id"] = criterion_id
        payload["checkpoint_id"] = body.checkpoint_id or criterion_id
        review = Review(
            id=new_id(),
            evaluation_id=evaluation.id,
            reviewer_user_id=actor_id,
            action=body.decision,
            reason=body.reason,
            payload_json=payload,
        )
        session.add(review)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.criterion.reviewed",
            entity_type="evaluation",
            entity_id=evaluation.id,
            reason=body.reason,
            before=None,
            after={"criterion_id": criterion_id, "decision": body.decision},
        )
        refresh_award_recommendation(session, project)
        return {"review_id": review.id, "status": "recorded"}


@review_router.get("/projects/{project_id}/review-log")
def review_log(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        bids = list(session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id)))
        entity_ids = {project.id, project.tender_id, project.tender_version_id}
        entity_ids.update(
            session.scalars(
                select(TenderVersion.id).where(TenderVersion.tender_id == project.tender_id)
            )
        )
        entity_ids.update(row.id for row in bids)
        bid_ids = {row.bid_id for row in bids}
        frontier = set(bid_ids)
        while frontier:
            parents = {
                value
                for value in session.scalars(
                    select(Bid.previous_bid_id).where(
                        Bid.tenant_id == principal.tenant_id,
                        Bid.id.in_(frontier),
                    )
                )
                if value and value not in bid_ids
            }
            bid_ids.update(parents)
            frontier = parents
        entity_ids.update(bid_ids)
        evaluation_ids = set(
            session.scalars(
                select(Evaluation.id).where(
                    Evaluation.tenant_id == principal.tenant_id,
                    Evaluation.bid_id.in_(bid_ids or {""}),
                )
            )
        )
        entity_ids.update(evaluation_ids)
        entity_ids.update(
            session.scalars(
                select(Document.id).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.tender_id == project.tender_id,
                )
            )
        )
        if bid_ids:
            entity_ids.update(
                session.scalars(
                    select(Document.id).where(
                        Document.tenant_id == principal.tenant_id,
                        Document.bid_id.in_(bid_ids),
                    )
                )
            )
        entity_ids.update(
            session.scalars(
                select(Clarification.id).where(
                    Clarification.tenant_id == principal.tenant_id,
                    Clarification.project_id == project.id,
                )
            )
        )
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.tenant_id == principal.tenant_id,
                    AuditEvent.entity_id.in_(entity_ids),
                )
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )
        records = [
            {
                "sequence": index,
                "action": event.event_type.rsplit(".", 1)[-1],
                "reason_code": event.event_type,
                "reason_text": event.reason or json.dumps(event.after_json or {}, sort_keys=True, default=str),
                "actor_id": event.actor_user_id or "system-worker",
                "actor_role": "system" if not event.actor_user_id else "reviewer",
                "subject_artifact_name": f"{event.entity_type}:{event.entity_id}",
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "request_id": event.request_id,
                "reason": event.reason,
                "before": event.before_json,
                "after": event.after_json,
                "recorded_at": event.occurred_at.isoformat(),
                "record_id": event.event_hash or event.id,
                "prior_record_id": event.previous_hash or "",
            }
            for index, event in enumerate(events, start=1)
        ]
        return {
            "ledger_namespace": f"tenant:{principal.tenant_id}",
            "record_count": len(records),
            "head_record_id": records[-1]["record_id"] if records else "",
            "records": records,
        }
