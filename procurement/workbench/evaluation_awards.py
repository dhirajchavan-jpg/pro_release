"""Award-recommendation inputs and persisted recommendation refresh."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from procurement.domain.models import (
    Evaluation,
    TenderVersion,
    WorkbenchBid,
    WorkbenchProject,
)
from procurement.evaluation.award import (
    apply_award_recommendation,
    public_award_view,
    source_hash,
)
from procurement.workbench.common import (
    _findings_block_qualification,
    _latest_reviews_by_criterion,
    _overlay_human_reviews_on_findings,
)


def _award_bidder_payload(session: Any, bid: WorkbenchBid, evaluation: Evaluation) -> dict[str, Any]:
    stored = dict(evaluation.internal_result_json or {})
    report = stored.get("report") if isinstance(stored.get("report"), dict) else {}
    vendor = report.get("vendor") if isinstance(report.get("vendor"), dict) else {}
    handoffs = stored.get("handoffs") if isinstance(stored.get("handoffs"), dict) else {}
    extensions = report.get("extensions") if isinstance(report.get("extensions"), dict) else {}
    reviews = _latest_reviews_by_criterion(session, evaluation)
    findings = _overlay_human_reviews_on_findings(report.get("findings") or [], reviews)
    qualifies = report.get("qualifies")
    if _findings_block_qualification(findings):
        qualifies = False
    return {
        "vendor_key": bid.vendor_key,
        "vendor_name": bid.vendor_name or vendor.get("vendor_name") or bid.vendor_key,
        "evaluation_id": evaluation.id,
        "qualifies": qualifies,
        "final_decision": evaluation.final_decision or report.get("final_decision"),
        "recommendation": report.get("recommendation"),
        "findings": findings,
        "next_ask": handoffs.get("08a_policy_next_ask") or extensions.get("policy_next_ask") or {},
        "financial_result": report.get("financial_result") or {},
    }


def collect_award_inputs(session: Any, project: WorkbenchProject) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bidders = list(
        session.scalars(
            select(WorkbenchBid)
            .where(WorkbenchBid.project_id == project.id)
            .order_by(WorkbenchBid.created_at)
        )
    )
    payload: list[dict[str, Any]] = []
    for bid in bidders:
        evaluation = session.get(Evaluation, bid.evaluation_id) if bid.evaluation_id else None
        if not evaluation or evaluation.status != "succeeded":
            continue
        payload.append(_award_bidder_payload(session, bid, evaluation))
    version = session.get(TenderVersion, project.tender_version_id)
    understanding = dict(version.understanding_json or {}) if version else {}
    tender = dict(understanding.get("tender_profile") or {})
    if project.title and not tender.get("tender_title"):
        tender["tender_title"] = project.title
    return payload, tender


def refresh_award_recommendation(session: Any, project: WorkbenchProject, *, force: bool = False) -> dict[str, Any] | None:
    bidders, tender = collect_award_inputs(session, project)
    if not bidders:
        return public_award_view(project.award_json)
    digest = source_hash(bidders, tender)
    stored = project.award_json if isinstance(project.award_json, dict) else {}
    if (
        not force
        and stored.get("source_hash") == digest
        and stored.get("officer_copy_source") in {"llm", "deterministic"}
    ):
        return public_award_view(stored)
    payload = apply_award_recommendation(bidders, tender=tender)
    project.award_json = payload
    project.updated_at = datetime.now(timezone.utc)
    return public_award_view(project.award_json)
