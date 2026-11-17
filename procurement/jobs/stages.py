"""Database-backed execution for the worker's four operation stages."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from procurement.platform.provider_usage import update_provider_usage


def run_understanding_operation(
    runtime: Any,
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
    work: Path,
    *,
    understand_tender: Callable[..., dict[str, Any]],
) -> bool:
    payload_pin = str(operation_payload.get("tender_version_id") or "").strip()
    with runtime.session_scope(sessions) as session:
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not project:
            raise ValueError("workbench project not found")
        tender = session.get(runtime.Tender, project.tender_id)
        pinned_version_id = runtime._started_tender_version_id(project, operation_payload)
        version = runtime._load_pinned_tender_version(session, project, pinned_version_id)
        if not payload_pin and runtime._tender_version_has_been_replaced(version):
            raise ValueError("tender version was superseded")
        prior_understanding = dict(version.understanding_json or {})
        schema = prior_understanding.get("field_schema")
        if not isinstance(schema, dict):
            raise ValueError("project field schema is missing")
        documents = list(
            session.scalars(
                runtime.select(runtime.Document).where(
                    runtime.Document.tender_version_id == pinned_version_id,
                    runtime.Document.tenant_id == project.tenant_id,
                runtime.Document.source_kind == "upload",
                ).order_by(runtime.Document.created_at, runtime.Document.id)
            )
        )
    tender_bundle = runtime._bundle(documents, work, "tender")

    def understanding_progress(stage: str, details: dict[str, object]) -> None:
        runtime._log_pipeline_stage(operation_id, "understanding", stage, **details)
        runtime._persist_operation_progress(
            sessions, operation_id, operation_lease_id, stage, details
        )

    def understanding_checkpoint(data: dict[str, Any]) -> None:
        with runtime.session_scope(sessions) as checkpoint_session:
            checkpoint_project = checkpoint_session.get(
                runtime.WorkbenchProject, entity_id
            )
            checkpoint_operation = checkpoint_session.get(runtime.Operation, operation_id)
            if not checkpoint_project or not checkpoint_operation:
                raise ValueError("understanding checkpoint target disappeared")
            if (
                checkpoint_operation.status != "running"
                or checkpoint_operation.lease_id != operation_lease_id
            ):
                raise ValueError("understanding operation lease is no longer active")
            if str(checkpoint_project.tender_version_id) != str(pinned_version_id):
                raise ValueError("tender version was superseded")
            checkpoint_version = checkpoint_session.get(
                runtime.TenderVersion, pinned_version_id
            )
            if not checkpoint_version:
                raise ValueError("understanding checkpoint version is missing")
            checkpoint_version.understanding_json = data
            if (
                checkpoint_operation.status == "running"
                and checkpoint_operation.lease_id == operation_lease_id
            ):
                checkpoint_operation.result_json = {
                    "progress": {
                        "stage": "field_understanding_checkpointed",
                        "result_status": (
                            data.get("field_understanding") or {}
                        ).get("status", "partial"),
                    },
                    "checkpoint": "field_understanding",
                    "attempt": checkpoint_operation.attempt,
                }

    result = understand_tender(
        operation_id,
        tender_bundle,
        work / "understand",
        schema,
        preview_context=(project.tenant_id, project.tender_id),
        progress=understanding_progress,
        checkpoint=understanding_checkpoint,
    )
    with runtime.session_scope(sessions) as session:
        operation = session.get(runtime.Operation, operation_id)
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not operation or not project:
            raise ValueError("understanding operation disappeared")
        session.refresh(project)
        version = runtime._load_pinned_tender_version(session, project, pinned_version_id)
        tender = session.get(runtime.Tender, project.tender_id)
        for carried in ("policy_audit", "compliance"):
            if carried not in result and carried in prior_understanding:
                result[carried] = prior_understanding[carried]
        version.understanding_json = result
        profile = dict(result.get("tender_profile") or {})
        extracted_title = str(profile.get("tender_title") or "").strip()
        extracted_number = str(profile.get("tender_number") or "").strip()
        if not project.title and extracted_title not in {"", "Unknown"}:
            project.title = extracted_title
        if tender and not tender.external_reference and extracted_number:
            tender.external_reference = extracted_number
        project.stage = "rfp_understood"
        for document in session.scalars(
            runtime.select(runtime.Document).where(
                runtime.Document.tender_version_id == pinned_version_id,
            )
        ):
            document.extraction_status = "extracted"
        runtime.finish(
            session,
            operation,
            {
                "project_id": project.id,
                "status": str(
                    (result.get("field_understanding") or {}).get("status")
                    or "partial"
                ),
            },
            lease_id=operation_lease_id,
        )
        runtime.audit(
            session,
            tenant_id=project.tenant_id,
            actor_user_id=None,
            event_type="workbench.understanding.completed",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"stage": "tender_uploaded"},
            after={"stage": project.stage},
        )
    runtime._log_pipeline_stage(
        operation_id,
        "understanding",
        "job_complete",
        result_status=(result.get("field_understanding") or {}).get("status"),
    )
    return True


def run_compliance_operation(
    runtime: Any,
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
) -> bool:
    payload_pin = str(operation_payload.get("tender_version_id") or "").strip()
    with runtime.session_scope(sessions) as session:
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not project:
            raise ValueError("workbench project not found")
        pinned_version_id = runtime._started_tender_version_id(project, operation_payload)
        version = runtime._load_pinned_tender_version(session, project, pinned_version_id)
        if not payload_pin and runtime._tender_version_has_been_replaced(version):
            raise ValueError("tender version was superseded")
        understanding = dict(version.understanding_json or {})
    from procurement.compliance.engine import run_compliance_audit

    def compliance_progress(stage: str, details: dict[str, object]) -> None:
        runtime._log_pipeline_stage(operation_id, "compliance", stage, **details)
        runtime._persist_operation_progress(
            sessions, operation_id, operation_lease_id, stage, details
        )

    policy_override = str(operation_payload.get("policy_id") or "").strip() or None
    result = run_compliance_audit(
        understanding, policy_id=policy_override, progress=compliance_progress
    )
    compliance_status = str(result.get("status") or "incomplete")
    with runtime.session_scope(sessions) as session:
        operation = session.get(runtime.Operation, operation_id)
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not operation or not project:
            raise ValueError("compliance operation disappeared")
        session.refresh(project)
        version = runtime._load_pinned_tender_version(session, project, pinned_version_id)
        data = dict(version.understanding_json or {})
        previous = (data.get("compliance") or {}).get("findings") or []
        resolutions = {
            str(item.get("finding_id")): item.get("resolution")
            for item in previous
            if isinstance(item, dict) and item.get("finding_id")
        }
        blocking = {"violation", "needs_review"}
        for finding in result.get("findings") or []:
            prior = resolutions.get(str(finding.get("finding_id")))
            if isinstance(prior, dict) and prior.get("status") in {"resolved", "waived"}:
                finding["resolution"] = prior
        summary = dict(result.get("summary") or {})
        summary["open"] = sum(
            1
            for finding in result.get("findings") or []
            if (finding.get("resolution") or {}).get("status") == "open"
            and finding.get("status") in blocking
        )
        result["summary"] = summary
        data["compliance"] = result
        version.understanding_json = data
        runtime.finish(
            session,
            operation,
            {
                "project_id": project.id,
                "status": compliance_status,
                "summary": summary,
            },
            lease_id=operation_lease_id,
        )
        runtime.audit(
            session,
            tenant_id=project.tenant_id,
            actor_user_id=None,
            event_type=f"workbench.compliance.{compliance_status}",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before=None,
            after={"status": compliance_status, "summary": summary},
        )
    runtime._log_pipeline_stage(
        operation_id,
        "compliance",
        "job_complete",
        result_status=compliance_status,
    )
    return True


def run_intake_operation(
    runtime: Any,
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
    work: Path,
    *,
    intake_bid: Callable[..., dict[str, Any]],
) -> bool:
    payload_version_id = str(operation_payload.get("tender_version_id") or "").strip()
    pinned_bid_id = str(operation_payload.get("bid_id") or "").strip()
    pinned_bid_ids = {
        str(value)
        for value in operation_payload.get("bid_ids") or []
        if str(value).strip()
    }
    with runtime.session_scope(sessions) as session:
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not project:
            raise ValueError("workbench project not found")
        pinned_version_id = runtime._started_tender_version_id(project, operation_payload)
        version = runtime._load_pinned_tender_version(session, project, pinned_version_id)
        if not payload_version_id and runtime._tender_version_has_been_replaced(version):
            raise ValueError("tender version was superseded")
        understanding = dict(version.understanding_json or {})
        project_rows = list(
            session.scalars(
                runtime.select(runtime.WorkbenchBid)
                .where(runtime.WorkbenchBid.project_id == project.id)
                .order_by(runtime.WorkbenchBid.created_at)
            )
        )
        vendor_key = str(operation_payload.get("vendor_key") or "")
        rows = runtime._select_intake_rows(project_rows, vendor_key)
        current_bid_ids = {str(row.bid_id) for row in project_rows}
        if pinned_bid_id:
            rows = [row for row in rows if str(row.bid_id) == pinned_bid_id]
            if pinned_bid_id not in current_bid_ids:
                raise ValueError("bid revision was superseded")
        elif pinned_bid_ids:
            rows = [row for row in rows if str(row.bid_id) in pinned_bid_ids]
            if not pinned_bid_ids.issubset(current_bid_ids):
                raise ValueError("bid revision was superseded")
        bid_inputs = []
        for row in rows:
            documents = list(
                session.scalars(
                    runtime.select(runtime.Document)
                    .where(
                        runtime.Document.bid_id == row.bid_id,
                        runtime.Document.tenant_id == project.tenant_id,
                    )
                    .order_by(runtime.Document.created_at, runtime.Document.id)
                )
            )
            bid_inputs.append(
                (row.id, row.vendor_key, documents, project.tenant_id, row.bid_id)
            )
    if not bid_inputs and not project_rows:
        raise ValueError("at least one bidder package is required")
    results: dict[str, dict] = {}
    for row_id, vendor_key, documents, tenant_id, bid_id in bid_inputs:
        update_provider_usage(operation_id, bid_id=bid_id, vendor_key=vendor_key)
        bundle = runtime._bundle(documents, work, f"bid-{vendor_key}")

        def intake_progress(
            stage: str,
            details: dict[str, object],
            current_vendor: str = vendor_key,
        ) -> None:
            runtime._log_pipeline_stage(
                operation_id,
                "classification",
                stage,
                vendor=current_vendor,
                **details,
            )
            runtime._persist_operation_progress(
                sessions,
                operation_id,
                operation_lease_id,
                stage,
                {"vendor": current_vendor, **details},
            )

        results[row_id] = intake_bid(
            bundle,
            work / f"intake-{vendor_key}",
            understanding=understanding,
            preview_context=(tenant_id, bid_id),
            progress=intake_progress,
        )
        with runtime.session_scope(sessions) as session:
            operation = session.get(runtime.Operation, operation_id)
            project = session.get(runtime.WorkbenchProject, entity_id)
            row = session.get(runtime.WorkbenchBid, row_id)
            if (
                not operation
                or operation.status != "running"
                or operation.lease_id != operation_lease_id
            ):
                raise ValueError("classification operation lease is no longer active")
            if not project or str(project.tender_version_id) != str(pinned_version_id):
                raise ValueError("tender version was superseded")
            if not row or str(row.bid_id) != str(bid_id):
                raise ValueError("bid revision was superseded")
            row.intake_json = results[row_id]
            row.classification_status = "review_required"
    with runtime.session_scope(sessions) as session:
        operation = session.get(runtime.Operation, operation_id)
        project = session.get(runtime.WorkbenchProject, entity_id)
        if not operation or not project:
            raise ValueError("intake operation disappeared")
        if str(project.tender_version_id) != str(pinned_version_id):
            raise ValueError("tender version was superseded")
        project_rows = list(
            session.scalars(
                runtime.select(runtime.WorkbenchBid)
                .where(runtime.WorkbenchBid.project_id == project.id)
                .order_by(runtime.WorkbenchBid.created_at)
            )
        )
        ready_count = sum(bool(row.intake_json) for row in project_rows)
        all_ready = bool(project_rows) and ready_count == len(project_rows)
        if all_ready:
            project.stage = "bids_intaken"
        runtime.finish(
            session,
            operation,
            {
                "project_id": project.id,
                "vendors": ready_count,
                "status": "complete" if all_ready else "partial",
            },
            lease_id=operation_lease_id,
        )
        runtime.audit(
            session,
            tenant_id=project.tenant_id,
            actor_user_id=None,
            event_type="workbench.intake.completed",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"stage": "contract_frozen"},
            after={"stage": project.stage, "vendors": ready_count},
        )
    runtime._log_pipeline_stage(
        operation_id,
        "classification",
        "job_complete",
        vendors=ready_count,
    )
    return True


def run_evaluation_operation(
    runtime: Any,
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_lease_id: str,
    work: Path,
    *,
    evaluate_reviewed_bid: Callable[..., tuple[Any, list[Any], dict[str, Any], int]],
) -> bool:
    evaluation_id = entity_id
    with runtime.session_scope(sessions) as session:
        evaluation = session.get(runtime.Evaluation, evaluation_id)
        if not evaluation:
            raise ValueError("evaluation not found")
        bid = session.get(runtime.Bid, evaluation.bid_id)
        version = session.get(runtime.TenderVersion, evaluation.tender_version_id)
        tender = session.get(runtime.Tender, version.tender_id) if version else None
        if not bid or not version or not tender:
            raise ValueError("evaluation references missing procurement records")
        bid_documents = list(
            session.scalars(
                runtime.select(runtime.Document)
                .where(
                    runtime.Document.bid_id == bid.id,
                    runtime.Document.tenant_id == evaluation.tenant_id,
                )
                .order_by(runtime.Document.created_at, runtime.Document.id)
            )
        )
        workbench_bid = session.scalar(
            runtime.select(runtime.WorkbenchBid).where(
                runtime.WorkbenchBid.bid_id == bid.id
            )
        )
        intake = dict(workbench_bid.intake_json or {}) if workbench_bid else {}
        understanding = dict(version.understanding_json or {})
        if not workbench_bid or workbench_bid.classification_status != "approved":
            raise ValueError("approved bidder classification is required")
        project = session.get(runtime.WorkbenchProject, workbench_bid.project_id)
        if not project or str(project.tender_version_id) != str(version.id):
            raise ValueError("evaluation inputs were superseded")
        if not (understanding.get("frozen_contract") or {}).get("criteria"):
            raise ValueError("frozen reviewed tender contract is required")
        evaluation.status = "running"
    bid_bundle = runtime._bundle(bid_documents, work, "bid")

    def update_progress(stage: str, phases: dict[str, float]) -> None:
        runtime._log_pipeline_stage(
            operation_id,
            "evaluation",
            stage,
            provider=os.getenv("AI_PROVIDER", "gemini"),
            model=os.getenv("AI_MODEL_DEEP") or os.getenv("GEMINI_MODEL_DEEP", ""),
            phase_seconds=json.dumps(phases, separators=(",", ":")),
        )
        with runtime.session_scope(sessions) as progress_session:
            progress_operation = progress_session.get(runtime.Operation, operation_id)
            if (
                progress_operation
                and progress_operation.status == "running"
                and progress_operation.lease_id == operation_lease_id
            ):
                progress_operation.result_json = {
                    "progress": {"stage": stage, "phase_seconds": phases}
                }
                progress_operation.progress_version = int(
                    progress_operation.progress_version or 0
                ) + 1

    update_progress("evaluation_started", {})
    report, _documents, handoffs, withheld_count = evaluate_reviewed_bid(
        evaluation_id,
        bid_bundle,
        work / "evaluate",
        understanding,
        intake,
        update_progress,
    )
    result = runtime._postgres_json_safe(runtime.simple_report(report))
    with runtime.session_scope(sessions) as session:
        operation = session.get(runtime.Operation, operation_id)
        evaluation = session.get(runtime.Evaluation, evaluation_id)
        if not operation or not evaluation:
            raise ValueError("operation disappeared")
        workbench_bid = session.scalar(
            runtime.select(runtime.WorkbenchBid).where(
                runtime.WorkbenchBid.bid_id == evaluation.bid_id
            )
        )
        project = (
            session.get(runtime.WorkbenchProject, workbench_bid.project_id)
            if workbench_bid
            else None
        )
        if (
            not workbench_bid
            or not project
            or str(project.tender_version_id) != str(evaluation.tender_version_id)
        ):
            raise ValueError("evaluation inputs were superseded")
        evaluation.status = "succeeded"
        evaluation.final_decision = str(result.get("final_decision") or "") or None
        evaluation.result_json = result
        evaluation.internal_result_json = runtime._postgres_json_safe({
            "report": runtime.model_dump(report),
            "handoffs": handoffs,
            "commercial_documents_withheld": withheld_count,
        })
        runtime.finish(
            session,
            operation,
            {"evaluation_id": evaluation.id, "status": evaluation.status},
            lease_id=operation_lease_id,
        )
        runtime.audit(
            session,
            tenant_id=evaluation.tenant_id,
            actor_user_id=None,
            event_type="evaluation.completed",
            entity_type="evaluation",
            entity_id=evaluation.id,
            reason=None,
            before={"status": "running"},
            after={"status": "succeeded"},
        )
        workbench_bid.evaluation_id = evaluation.id
        if project:
            project_bids = list(
                session.scalars(
                    runtime.select(runtime.WorkbenchBid).where(
                        runtime.WorkbenchBid.project_id == project.id
                    )
                )
            )
            complete = bool(project_bids) and all(
                row.evaluation_id
                and (completed := session.get(runtime.Evaluation, row.evaluation_id))
                and completed.status == "succeeded"
                for row in project_bids
            )
            if complete:
                try:
                    from procurement.workbench.evaluation import refresh_award_recommendation

                    refresh_award_recommendation(session, project, force=True)
                except Exception:
                    runtime.logger.exception(
                        "award suggestion failed project_id=%s", project.id
                    )
            if complete and project.stage != "evaluated":
                previous_stage = project.stage
                project.stage = "evaluated"
                project.updated_at = runtime.datetime.now(runtime.timezone.utc)
                runtime.audit(
                    session,
                    tenant_id=project.tenant_id,
                    actor_user_id=None,
                    event_type="workbench.evaluation.completed",
                    entity_type="workbench_project",
                    entity_id=project.id,
                    reason=None,
                    before={"stage": previous_stage},
                    after={"stage": project.stage},
                )
    runtime._log_pipeline_stage(
        operation_id,
        "evaluation",
        "job_complete",
        withheld_documents=withheld_count,
    )
    return True
