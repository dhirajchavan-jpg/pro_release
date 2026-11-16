"""Durable polling worker and compatibility facade for pipeline jobs.

The worker owns leasing, restart recovery, and stage dispatch. The three
domain pipelines live under :mod:`procurement.pipelines`; the underscored
functions below are deliberate compatibility seams for the existing jobs and
tests.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from ai_provider import AIProviderFailure
from procurement.domain.models import (
    Bid,
    Document,
    Evaluation,
    Operation,
    Tender,
    TenderVersion,
    WorkbenchBid,
    WorkbenchProject,
)
from procurement.domain.serialization import model_dump
from procurement.evaluation import document_verifier as document_verifier_runtime
from procurement.evaluation import ocr_recovery as ocr_recovery_runtime
from procurement.evaluation import pipeline as evaluation_pipeline_runtime
from procurement.evaluation.assessment import decision_agent
from procurement.evaluation.criteria import normalize_prohibition_criterion
from procurement.evaluation.evidence_pipeline import (
    cross_document_reconciliation_agent,
    evidence_ledger_builder,
    evidence_packet_builder_agent,
    specialist_orchestrator_agent,
)
from procurement.evaluation.evidence_scout import (
    evidence_mapper_agent,
    grouped_evidence_scout_agent,
)
from procurement.evaluation.planning import normalize_requirement_checks, resolved_evidence_type
from procurement.evaluation.reporting import simple_report
from procurement.evaluation.semantic_retrieval import bidder_semantic_index
from procurement.evaluation.validation import enforce_server_validation
from procurement.evaluation.vendor_profile import extract_vendor_profile
from procurement.jobs import dispatcher as job_dispatcher
from procurement.jobs import stages as job_stages
from procurement.pipelines.bid_evaluation import (
    EvaluationHooks,
    approved_requirement_matrix,
    evaluate_reviewed_bid,
    restore_bidder_index_labels,
)
from procurement.pipelines.bidder_intake import (
    BidderIntakeHooks,
    intake_bid,
    technical_bid_bundle,
)
from procurement.pipelines.job_runtime import DispatcherRuntime, StageRuntime
from procurement.pipelines.lifecycle import (
    _is_retryable_failure,
    _log_pipeline_stage,
    _persist_operation_progress,
    _public_failure_detail,
    _select_intake_rows,
    restore_previous_successful_evaluation,
)
from procurement.pipelines.tender_understanding import (
    TenderUnderstandingHooks,
    understand_tender,
)
from procurement.pipelines.support import (
    _bid_tender_match,
    _bundle as _support_bundle,
    _canonical_evidence_groups,
    _canonical_evidence_id_groups,
    _gem_bid_ids,
    _is_price_document_type,
    _logical_classification_items,
    _logical_page_ids,
    _logical_source_ids,
    _ocr_gap_message,
    _postgres_json_safe,
    _preserve_ocr_uncertainty,
    _profile_data_from_understanding,
    _raise_on_ocr_errors,
    _store_preview_sources as _support_store_preview_sources,
    commercial_quarantine,
)
from procurement.platform.database import make_session_factory, session_scope
from procurement.platform.env import load_local_env
from procurement.platform.ingestion import unpack_upload
from procurement.platform.job_context import (
    check_job_deadline,
    clear_job_deadline,
    llm_usage_summary,
    reset_llm_usage,
    set_job_deadline,
)
from procurement.platform.legacy_jobs import artifact_dir_for
from procurement.platform.object_store import download_file, put_file
from procurement.platform.operations import (
    OperationLeaseError,
    audit,
    claim_next,
    fail,
    finish,
    heartbeat,
    recover_interrupted,
    retry_later,
)
from procurement.platform.provider_usage import (
    persist_provider_call,
    start_provider_usage,
    stop_provider_usage,
)
from procurement.understanding import service as understanding_service
from procurement.understanding.classification import (
    classify_documents,
    classify_pages,
    reconstruct_logical_documents,
    regroup_logical_documents,
    summarize_logical_documents,
)
from procurement.understanding.documents import load_upload_documents
from procurement.understanding.ocr_pipeline import OCR_RESULT_CACHE_DIR, run_base_rfp_ocr
from procurement.understanding.sources import identify_base_rfp_set
from procurement.understanding.structure import reconstruct_document_structure
from procurement.understanding.structural_review import (
    BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED,
    run_structural_adjudication,
)
from procurement.workbench.common import _tender_version_has_been_replaced


logger = logging.getLogger("procurement.worker")


def _store_preview_sources(
    documents: list,
    tenant_id: str,
    owner_id: str,
    storage_group: str = "source-previews",
) -> dict[str, dict]:
    """Keep the historic seam while allowing tests to replace ``put_file``."""
    return _support_store_preview_sources(
        documents,
        tenant_id,
        owner_id,
        storage_group=storage_group,
        put=put_file,
    )


def _bundle(documents: list[Document], root: Path, name: str) -> Path:
    """Keep the historic seam while allowing tests to replace ``download_file``."""
    return _support_bundle(documents, root, name, download=download_file)


def _understand_tender(
    operation_id: str,
    tender_bundle: Path,
    work: Path,
    field_schema: dict,
    *,
    preview_context: tuple[str, str] | None = None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Compatibility facade for the tender-understanding pipeline."""
    return understand_tender(
        operation_id,
        tender_bundle,
        work,
        field_schema,
        preview_context=preview_context,
        progress=progress,
        checkpoint=checkpoint,
        hooks=TenderUnderstandingHooks(
            load_upload_documents=load_upload_documents,
            run_base_rfp_ocr=run_base_rfp_ocr,
            classify_documents=classify_documents,
            classify_pages=classify_pages,
            reconstruct_logical_documents=reconstruct_logical_documents,
            reconstruct_document_structure=reconstruct_document_structure,
            identify_base_rfp_set=identify_base_rfp_set,
            run_structural_adjudication=run_structural_adjudication,
            artifact_dir_for=artifact_dir_for,
            model_dump=model_dump,
            store_preview_sources=_store_preview_sources,
            profile_from_fields=understanding_service.profile_from_fields,
            requirement_matrix_from_fields=understanding_service.requirement_matrix_from_fields,
            run_field_understanding=understanding_service.run_field_understanding,
            structural_adjudication_enabled=BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED,
        ),
    )


def _intake_bid(
    bid_bundle: Path,
    work: Path,
    *,
    understanding: dict[str, Any] | None = None,
    preview_context: tuple[str, str] | None = None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
) -> dict:
    """Compatibility facade for bidder intake and classification."""
    return intake_bid(
        bid_bundle,
        work,
        understanding=understanding,
        preview_context=preview_context,
        progress=progress,
        hooks=BidderIntakeHooks(
            load_upload_documents=load_upload_documents,
            run_base_rfp_ocr=run_base_rfp_ocr,
            classify_documents=classify_documents,
            classify_pages=classify_pages,
            reconstruct_logical_documents=reconstruct_logical_documents,
            regroup_logical_documents=regroup_logical_documents,
            summarize_logical_documents=summarize_logical_documents,
            model_dump=model_dump,
            store_preview_sources=_store_preview_sources,
        ),
    )


def _technical_bid_bundle(bundle: Path, intake: dict, work: Path) -> tuple[Path, int]:
    """Compatibility facade for commercial-document quarantine."""
    return technical_bid_bundle(bundle, intake, work)


def _approved_requirement_matrix(understanding: dict[str, Any]):
    """Compatibility facade for the frozen checklist compiler."""
    return approved_requirement_matrix(understanding)


def _restore_bidder_index_labels(logical_documents: list[Any], documents: list[Any]) -> None:
    """Compatibility facade for rebuilding derived bidder routing labels."""
    return restore_bidder_index_labels(logical_documents, documents)


def _evaluate_reviewed_bid(
    evaluation_id: str,
    bid_bundle: Path,
    work: Path,
    understanding: dict,
    intake: dict,
    progress: Callable[[str, dict[str, float]], None] | None = None,
) -> tuple:
    """Compatibility facade for evaluation against the frozen checklist."""
    return evaluate_reviewed_bid(
        evaluation_id,
        bid_bundle,
        work,
        understanding,
        intake,
        progress=progress,
        hooks=EvaluationHooks(
            document_verifier_agent=document_verifier_runtime.document_verifier_agent,
            run_ocr_candidates=ocr_recovery_runtime.run_ocr_candidates,
            run_criteria_evaluation_stage=evaluation_pipeline_runtime.run_criteria_evaluation_stage,
            extract_vendor_profile=extract_vendor_profile,
            grouped_evidence_scout_agent=grouped_evidence_scout_agent,
            evidence_mapper_agent=evidence_mapper_agent,
            evidence_packet_builder_agent=evidence_packet_builder_agent,
            evidence_ledger_builder=evidence_ledger_builder,
            specialist_orchestrator_agent=specialist_orchestrator_agent,
            cross_document_reconciliation_agent=cross_document_reconciliation_agent,
            enforce_server_validation=enforce_server_validation,
            check_job_deadline=check_job_deadline,
            reset_llm_usage=reset_llm_usage,
            llm_usage_summary=llm_usage_summary,
            model_dump=model_dump,
        ),
    )


def _started_tender_version_id(project: Any, payload: dict[str, Any] | None) -> str:
    pinned = str((payload or {}).get("tender_version_id") or "").strip()
    return pinned or str(project.tender_version_id)


def _load_pinned_tender_version(session: Any, project: Any, pinned_version_id: str) -> Any:
    if str(getattr(project, "tender_version_id", "") or "") != str(pinned_version_id):
        raise ValueError("tender version was superseded")
    version = session.get(TenderVersion, pinned_version_id)
    if not version:
        raise ValueError("project tender version is missing")
    return version


def _stage_dependencies() -> StageRuntime:
    """Build the explicit compatibility adapter consumed by ``jobs.stages``."""
    return StageRuntime(
        Bid=Bid,
        Document=Document,
        Evaluation=Evaluation,
        Operation=Operation,
        Tender=Tender,
        TenderVersion=TenderVersion,
        WorkbenchBid=WorkbenchBid,
        WorkbenchProject=WorkbenchProject,
        audit=audit,
        datetime=datetime,
        finish=finish,
        logger=logger,
        model_dump=model_dump,
        select=select,
        session_scope=session_scope,
        simple_report=simple_report,
        timezone=timezone,
        _bundle=_bundle,
        _load_pinned_tender_version=_load_pinned_tender_version,
        _log_pipeline_stage=_log_pipeline_stage,
        _persist_operation_progress=_persist_operation_progress,
        _postgres_json_safe=_postgres_json_safe,
        _select_intake_rows=_select_intake_rows,
        _started_tender_version_id=_started_tender_version_id,
        _tender_version_has_been_replaced=_tender_version_has_been_replaced,
    )


def _run_understanding_operation(
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
    work: Path,
) -> bool:
    return job_stages.run_understanding_operation(
        _stage_dependencies(),
        sessions,
        operation_id,
        entity_id,
        operation_payload,
        operation_lease_id,
        work,
        understand_tender=_understand_tender,
    )


def _run_compliance_operation(
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
) -> bool:
    return job_stages.run_compliance_operation(
        _stage_dependencies(),
        sessions,
        operation_id,
        entity_id,
        operation_payload,
        operation_lease_id,
    )


def _run_intake_operation(
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_payload: dict[str, Any],
    operation_lease_id: str,
    work: Path,
) -> bool:
    return job_stages.run_intake_operation(
        _stage_dependencies(),
        sessions,
        operation_id,
        entity_id,
        operation_payload,
        operation_lease_id,
        work,
        intake_bid=_intake_bid,
    )


def _run_evaluation_operation(
    sessions: Any,
    operation_id: str,
    entity_id: str,
    operation_lease_id: str,
    work: Path,
) -> bool:
    def evaluate_reviewed_bid(*args: Any, **kwargs: Any) -> Any:
        return _evaluate_reviewed_bid(*args, **kwargs)

    return job_stages.run_evaluation_operation(
        _stage_dependencies(),
        sessions,
        operation_id,
        entity_id,
        operation_lease_id,
        work,
        evaluate_reviewed_bid=evaluate_reviewed_bid,
    )


def _dispatcher_dependencies() -> DispatcherRuntime:
    """Build the explicit compatibility adapter consumed by ``jobs.dispatcher``."""
    return DispatcherRuntime(
        Evaluation=Evaluation,
        Operation=Operation,
        OperationLeaseError=OperationLeaseError,
        audit=audit,
        claim_next=claim_next,
        clear_job_deadline=clear_job_deadline,
        fail=fail,
        heartbeat=heartbeat,
        load_local_env=load_local_env,
        make_session_factory=make_session_factory,
        recover_interrupted=recover_interrupted,
        reset_llm_usage=reset_llm_usage,
        restore_previous_successful_evaluation=restore_previous_successful_evaluation,
        retry_later=retry_later,
        run_once=run_once,
        select=select,
        session_scope=session_scope,
        set_job_deadline=set_job_deadline,
        start_provider_usage=start_provider_usage,
        stop_provider_usage=stop_provider_usage,
        _heartbeat_forever=_heartbeat_forever,
        _is_retryable_failure=_is_retryable_failure,
        _log_pipeline_stage=_log_pipeline_stage,
        _operation_heartbeat=_operation_heartbeat,
        _public_failure_detail=_public_failure_detail,
        _provider_usage_context=_provider_usage_context,
        _persist_provider_call=persist_provider_call,
        _run_compliance_operation=_run_compliance_operation,
        _run_evaluation_operation=_run_evaluation_operation,
        _run_intake_operation=_run_intake_operation,
        _run_once_safely=_run_once_safely,
        _run_understanding_operation=_run_understanding_operation,
    )


def _provider_usage_context(
    sessions: Any,
    operation_id: str,
    operation_attempt: int,
    stage: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "operation_attempt": operation_attempt,
        "stage": stage,
        "project_id": payload.get("project_id"),
        "tender_version_id": payload.get("tender_version_id"),
        "bid_id": payload.get("bid_id"),
        "evaluation_id": payload.get("evaluation_id"),
    }
    with session_scope(sessions) as session:
        operation = session.get(Operation, operation_id)
        if not operation:
            raise ValueError("provider usage operation is missing")
        context["tenant_id"] = operation.tenant_id
        if operation.entity_type == "workbench_project":
            context["project_id"] = context.get("project_id") or entity_id
        elif operation.entity_type == "evaluation":
            evaluation = session.get(Evaluation, entity_id)
            if evaluation:
                context.update(
                    {
                        "evaluation_id": evaluation.id,
                        "tender_version_id": evaluation.tender_version_id,
                        "bid_id": evaluation.bid_id,
                    }
                )
                row = session.scalar(
                    select(WorkbenchBid).where(WorkbenchBid.bid_id == evaluation.bid_id)
                )
                if row:
                    context["project_id"] = row.project_id
    return context


def _heartbeat_forever() -> None:
    job_dispatcher.heartbeat_forever()


def _operation_heartbeat(
    sessions: Any,
    operation_id: str,
    lease_id: str,
    stop: threading.Event,
) -> None:
    job_dispatcher.operation_heartbeat(
        _dispatcher_dependencies(), sessions, operation_id, lease_id, stop
    )


def _run_once_safely(run: Callable[[], bool] | None = None) -> bool:
    return job_dispatcher.run_once_safely(_dispatcher_dependencies(), run)


def run_once(sessions: Any | None = None) -> bool:
    return job_dispatcher.run_once(_dispatcher_dependencies(), sessions)


def run_forever() -> None:
    job_dispatcher.run_forever(_dispatcher_dependencies())


if __name__ == "__main__":
    run_forever()
