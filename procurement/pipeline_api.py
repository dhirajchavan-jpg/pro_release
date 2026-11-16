"""Stable external API over the proven workbench worker stages."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import select

from procurement.api import _actor_user_id, _ensure_bid_capacity, _owned, _sessions, _store_document, _upload_sha256
from procurement.domain.models import Bid, Document, Evaluation, IdempotencyKey, Operation, Tender, TenderVersion, WorkbenchBid, WorkbenchProject, new_id
from procurement.domain.schemas import PublicEvaluationResult
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import IdempotencyConflict, audit, enqueue
from procurement.workbench.common import _classification_payload, _idempotency_key, _key, _ledger, _ledger_with_urls, _project, _schema_from_text, _schema_projection, _source_urls


class EvaluationStart(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    vendor_id: str = Field(min_length=1, max_length=120)


class PipelineAccepted(BaseModel):
    api_version: Literal["procurement-pipeline.v1"] = "procurement-pipeline.v1"
    job_id: str
    status: Literal["queued", "running", "complete", "failed"]
    job_url: str
    project_id: str
    tender_id: str | None = None
    tender_version_id: str | None = None
    bid_id: str | None = None
    vendor_id: str | None = None
    evaluation_id: str | None = None


class PipelineEvidence(BaseModel):
    source_id: str = ""
    document_id: str = ""
    page_id: str = ""
    page: int | None = None
    quote: str = ""
    document_url: str | None = None
    pdf_url: str | None = None
    page_image_url: str | None = None
    page_url: str | None = None


class PipelineLedgerEntry(BaseModel):
    key: str
    label: str
    requirement_level: str
    status: str
    value: JsonValue | None = None
    conditions: list[JsonValue] = Field(default_factory=list)
    exceptions: list[JsonValue] = Field(default_factory=list)
    required_documents: list[JsonValue] = Field(default_factory=list)
    evidence: list[PipelineEvidence] = Field(default_factory=list)
    verified: bool
    review_source: str
    blocking_grounding_issues: list[str] = Field(default_factory=list)
    notes: str = ""


class PipelineKeyFact(BaseModel):
    label: str
    value: str


class PipelineTenderSummary(BaseModel):
    text: str
    source: str
    key_facts: list[PipelineKeyFact] = Field(default_factory=list)


class PipelineFieldSchema(BaseModel):
    schema_id: str
    field_count: int
    section_count: int
    sections: list[str]


class PipelineLedger(BaseModel):
    ledger_status: Literal["complete", "needs_review"]
    editable: bool
    tender_summary: PipelineTenderSummary
    entries: list[PipelineLedgerEntry]
    warnings: list[JsonValue] = Field(default_factory=list)


class PipelineDocument(BaseModel):
    document_id: str
    file_name: str
    content_type: str
    page_count: int | None = None
    content_url: str | None = None
    page_image_url_template: str | None = None


class PipelineLinks(BaseModel):
    ledger: str | None = None
    contract_draft: str | None = None
    contract_freeze: str | None = None
    classification: str | None = None
    classification_correct: str | None = None
    classification_approve: str | None = None
    evaluation_results: str | None = None
    report: str | None = None


class TenderUnderstandingResult(BaseModel):
    schema_version: Literal["procurement-understanding.v1"] = "procurement-understanding.v1"
    project_id: str
    tender_id: str
    tender_version_id: str
    review_status: Literal["review_required", "approved"]
    field_schema: PipelineFieldSchema
    ledger: PipelineLedger
    documents: list[PipelineDocument]
    links: PipelineLinks


class PipelineLogicalDocument(BaseModel):
    logical_document_id: str
    title: str
    document_type: str
    source_document_id: str
    start_page: int
    end_page: int
    content_summary: str
    key_facts: list[JsonValue] = Field(default_factory=list)
    confidence: float


class PipelinePhysicalRoute(BaseModel):
    document_id: str
    broad_route: str
    reason: str = ""


class BidClassificationResult(BaseModel):
    schema_version: Literal["procurement-classification.v1"] = "procurement-classification.v1"
    project_id: str
    bid_id: str
    vendor_id: str
    vendor_name: str
    review_status: Literal["review_required", "approved"]
    document_types: list[str]
    documents: list[PipelineDocument]
    logical_documents: list[PipelineLogicalDocument]
    physical_routes: list[PipelinePhysicalRoute]
    links: PipelineLinks


class BidEvaluationResult(BaseModel):
    schema_version: Literal["procurement-pipeline-evaluation.v1"] = "procurement-pipeline-evaluation.v1"
    project_id: str
    bid_id: str
    vendor_id: str
    evaluation_id: str
    evaluation: PublicEvaluationResult
    links: PipelineLinks


PipelineResult = TenderUnderstandingResult | BidClassificationResult | BidEvaluationResult


class PipelineJob(BaseModel):
    api_version: Literal["procurement-pipeline.v1"] = "procurement-pipeline.v1"
    job_id: str
    stage: Literal["understanding", "intake", "evaluation"]
    status: Literal["queued", "running", "complete", "failed"]
    result_type: Literal["tender_understanding", "bid_classification", "bid_evaluation"] | None = None
    error_code: str | None = None
    error: str | None = None
    result: PipelineResult | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


router = APIRouter(
    prefix="/v1/pipeline",
    tags=["pipeline"],
    responses={
        code: {"model": ErrorEnvelope}
        for code in (400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 503)
    },
)


def _accepted(request: Request, operation: Operation, **ids: str) -> dict[str, Any]:
    return {
        **ids,
        "job_id": operation.id,
        "status": "complete" if operation.status == "succeeded" else operation.status,
        "job_url": f"{str(request.base_url).rstrip('/')}/v1/pipeline/jobs/{operation.id}",
    }


def _base_url(request: Request) -> str:
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")


def _tender_result(session: Any, project: WorkbenchProject, version: TenderVersion, request: Request) -> dict[str, Any]:
    data = dict(version.understanding_json or {})
    base_url = _base_url(request)
    stored = list(
        session.scalars(
            select(Document).where(
                Document.tender_version_id == version.id,
                Document.tenant_id == project.tenant_id,
            )
        )
    )
    urls = _source_urls(data, stored, project.id, base_url)
    documents = [
        {
            "document_id": source_id,
            "file_name": str(source.get("file_name") or f"{source_id}.pdf"),
            "content_type": str(source.get("content_type") or "application/pdf"),
            "page_count": int(source.get("page_count") or 0) or None,
            "content_url": pair[0],
            "page_image_url_template": pair[1],
        }
        for source_id, pair in urls.items()
        for source in [(data.get("preview_sources") or {}).get(source_id) or {}]
    ]
    return {
        "project_id": project.id,
        "tender_id": project.tender_id,
        "tender_version_id": version.id,
        "review_status": "approved" if version.status == "approved" else "review_required",
        "field_schema": _schema_projection(data.get("field_schema") or _schema_from_text(None)),
        "ledger": _ledger_with_urls(_ledger(data), urls),
        "documents": documents,
        "links": {
            "ledger": f"{base_url}/v1/workbench/projects/{project.id}/ledger",
            "contract_draft": f"{base_url}/v1/workbench/projects/{project.id}/contract/draft",
            "contract_freeze": f"{base_url}/v1/workbench/projects/{project.id}/contract/freeze",
        },
    }


def _classification_result(
    session: Any,
    project: WorkbenchProject,
    bidder: WorkbenchBid,
    request: Request,
) -> dict[str, Any]:
    payload = _classification_payload(bidder)
    intake = dict(bidder.intake_json or {})
    previews = intake.get("preview_sources") or {}
    base_url = _base_url(request)
    stored = list(
        session.scalars(
            select(Document).where(
                Document.bid_id == bidder.bid_id,
                Document.tenant_id == project.tenant_id,
            )
        )
    )
    only_stored = stored[0] if len(stored) == 1 else None
    documents = []
    for item in intake.get("documents") or []:
        source_id = str(item.get("document_id") or "")
        preview = previews.get(source_id) or {}
        if preview.get("object_key"):
            base = f"{base_url}/v1/workbench/projects/{project.id}/bids/{bidder.vendor_key}/sources/{source_id}"
            content_url = f"{base}/content"
            page_template = f"{base}/pages/{{page}}.png"
        else:
            source = next((row for row in stored if row.id == source_id), None) or only_stored
            content_url = f"{base_url}/v1/workbench/documents/{source.id}/content" if source else None
            page_template = (
                f"{base_url}/v1/workbench/documents/{source.id}/pages/{{page}}.png"
                if source and Path(source.file_name).suffix.lower() == ".pdf"
                else None
            )
        documents.append(
            {
                "document_id": source_id,
                "file_name": str(item.get("file_name") or source_id),
                "content_type": str(preview.get("content_type") or "application/pdf"),
                "page_count": int(item.get("page_count") or 0) or None,
                "content_url": content_url,
                "page_image_url_template": page_template,
            }
        )
    classification = payload["classification"]
    prefix = f"{base_url}/v1/workbench/projects/{project.id}/bids/{bidder.vendor_key}"
    return {
        "project_id": project.id,
        "bid_id": bidder.bid_id,
        "vendor_id": bidder.vendor_key,
        "vendor_name": bidder.vendor_name,
        "review_status": "approved" if bidder.classification_status == "approved" else "review_required",
        "document_types": payload["document_types"],
        "documents": documents,
        "logical_documents": classification["logical_documents"],
        "physical_routes": classification["physical_routes"],
        "links": {
            "classification": f"{prefix}/classification",
            "classification_correct": f"{prefix}/classification/correct",
            "classification_approve": f"{prefix}/classification/approve",
        },
    }


def _request_hash(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _idempotent_operation(
    session: Any,
    tenant_id: str,
    key: str | None,
    request_hash: str,
) -> Operation | None:
    if not key:
        return None
    record = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
        )
    )
    if not record:
        return None
    if record.request_hash != request_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different request")
    operation = session.get(Operation, record.operation_id)
    if not operation:
        raise HTTPException(status_code=409, detail="idempotency record is inconsistent")
    return operation


def _active_classification_operation(session: Any, project_id: str, vendor_key: str) -> Operation | None:
    """Find the active intake operation covering one bidder.

    The worker still expects intake operations to point at the project. The
    bidder scope is carried in the operation payload; a missing vendor key is
    the legacy project-wide intake and therefore covers every bidder.
    """
    operations = session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "workbench_project",
            Operation.entity_id == project_id,
            Operation.stage == "intake",
            Operation.status.in_(("queued", "running")),
        )
        .order_by(Operation.created_at.desc())
    )
    for operation in operations:
        payload = operation.payload_json or {}
        operation_vendor = str(payload.get("vendor_key") or "")
        if not operation_vendor or operation_vendor == vendor_key:
            return operation
    return None


@router.post(
    "/tenders/understand",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PipelineAccepted,
    response_model_exclude_none=True,
)
async def start_tender_understanding(
    request: Request,
    file: UploadFile = File(...),
    field_schema_json: str | None = Form(None, alias="schema_json"),
    project_key: str | None = Form(None),
    title: str | None = Form(None),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _key(project_key or f"{file.filename or 'tender'}-{new_id()[:8]}")
    schema = _schema_from_text(field_schema_json)
    idempotency_key = _idempotency_key(request, required=True)
    request_hash = _request_hash(
        "understanding",
        key,
        (title or "").strip(),
        _upload_sha256(file),
        json.dumps(schema, sort_keys=True, separators=(",", ":")),
    )
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        existing = _idempotent_operation(session, principal.tenant_id, idempotency_key, request_hash)
        if existing:
            project = session.get(WorkbenchProject, existing.entity_id)
            version = session.get(TenderVersion, project.tender_version_id) if project else None
            if not project or not version:
                raise HTTPException(status_code=409, detail="idempotent understanding job is inconsistent")
            return _accepted(request, existing, project_id=project.id, tender_id=project.tender_id, tender_version_id=version.id)
        if session.scalar(select(WorkbenchProject.id).where(WorkbenchProject.tenant_id == principal.tenant_id, WorkbenchProject.project_key == key)):
            raise HTTPException(status_code=409, detail="project key already exists")
        tender = Tender(id=new_id(), tenant_id=principal.tenant_id, external_reference=key, status="draft")
        session.add(tender)
        session.flush([tender])
        version = TenderVersion(id=new_id(), tender_id=tender.id, version_number=1, status="draft", understanding_json={"field_schema": schema})
        session.add(version)
        session.flush([version])
        document = await _store_document(
            session,
            file,
            tenant_id=principal.tenant_id,
            actor_id=actor_id,
            tender_id=tender.id,
            tender_version_id=version.id,
            bid_id=None,
        )
        project = WorkbenchProject(
            id=new_id(),
            tenant_id=principal.tenant_id,
            tender_id=tender.id,
            tender_version_id=version.id,
            project_key=key,
            title=(title or "").strip() or None,
            stage="tender_uploaded",
        )
        session.add(project)
        session.flush([project])
        try:
            operation = enqueue(
                session,
                tenant_id=principal.tenant_id,
                entity_type="workbench_project",
                entity_id=project.id,
                stage="understanding",
                payload={"project_id": project.id, "tender_version_id": version.id},
                key=idempotency_key,
                request_hash=request_hash,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="pipeline.understanding.queued",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before=None,
            after={"document_id": document.id, "operation_id": operation.id},
        )
        return _accepted(request, operation, project_id=project.id, tender_id=tender.id, tender_version_id=version.id)


@router.post(
    "/bids/classify",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PipelineAccepted,
    response_model_exclude_none=True,
)
async def start_bid_classification(
    request: Request,
    file: UploadFile = File(...),
    project_id: str = Form(...),
    vendor_name: str = Form(...),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    vendor_name = vendor_name.strip()
    if not vendor_name:
        raise HTTPException(status_code=422, detail="vendor_name is required")
    vendor_key = _key(vendor_name)
    idempotency_key = _idempotency_key(request, required=True)
    request_hash = _request_hash(
        "classification",
        project_id,
        vendor_key,
        _upload_sha256(file),
    )
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = session.scalar(
            select(WorkbenchProject)
            .where(WorkbenchProject.id == project_id, WorkbenchProject.tenant_id == principal.tenant_id)
            .with_for_update()
        )
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        existing = _idempotent_operation(session, principal.tenant_id, idempotency_key, request_hash)
        if existing:
            bidder = session.scalar(
                select(WorkbenchBid)
                .where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_key)
                .with_for_update()
            )
            if not bidder:
                raise HTTPException(status_code=409, detail="idempotent classification job is inconsistent")
            return _accepted(request, existing, project_id=project.id, bid_id=bidder.bid_id, vendor_id=vendor_key)
        version = session.get(TenderVersion, project.tender_version_id)
        if not version or version.status != "approved":
            raise HTTPException(status_code=409, detail="approve the reviewed tender version before classifying bids")
        if not ((version.understanding_json or {}).get("frozen_contract") or {}).get("criteria"):
            raise HTTPException(status_code=409, detail="finalize the evaluation checklist before classifying bids")
        existing_bidder = session.scalar(
            select(WorkbenchBid)
            .where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_key)
            .with_for_update()
        )
        if existing_bidder:
            active = _active_classification_operation(session, project.id, vendor_key)
            if active:
                return _accepted(
                    request,
                    active,
                    project_id=project.id,
                    bid_id=existing_bidder.bid_id,
                    vendor_id=vendor_key,
                )
            raise HTTPException(status_code=409, detail="bidder already exists in this project")
        _ensure_bid_capacity(session, version.id, principal.tenant_id)
        bid = Bid(id=new_id(), tenant_id=principal.tenant_id, tender_version_id=version.id, bidder_name=vendor_name, status="uploaded")
        session.add(bid)
        session.flush([bid])
        document = await _store_document(session, file, tenant_id=principal.tenant_id, actor_id=actor_id, tender_id=None, bid_id=bid.id)
        bidder = WorkbenchBid(id=new_id(), project_id=project.id, bid_id=bid.id, vendor_key=vendor_key, vendor_name=vendor_name)
        session.add(bidder)
        session.flush([bidder])
        try:
            operation = enqueue(
                session,
                tenant_id=principal.tenant_id,
                entity_type="workbench_project",
                entity_id=project.id,
                stage="intake",
                payload={
                    "project_id": project.id,
                    "tender_version_id": version.id,
                    "vendor_key": vendor_key,
                    "bid_id": bid.id,
                },
                key=idempotency_key,
                request_hash=request_hash,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="pipeline.classification.queued",
            entity_type="workbench_bid",
            entity_id=bidder.id,
            reason=None,
            before=None,
            after={"document_id": document.id, "operation_id": operation.id},
        )
        return _accepted(request, operation, project_id=project.id, bid_id=bid.id, vendor_id=vendor_key)


@router.post(
    "/bids/evaluate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PipelineAccepted,
    response_model_exclude_none=True,
)
def start_bid_evaluation(body: EvaluationStart, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    idempotency_key = _idempotency_key(request, required=True)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, body.project_id, principal.tenant_id)
        bidder = session.scalar(
            select(WorkbenchBid)
            .where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == body.vendor_id)
            .with_for_update()
        )
        if not bidder:
            raise HTTPException(status_code=404, detail="bidder not found")
        request_hash = _request_hash(
            "evaluation",
            body.project_id,
            project.tender_version_id,
            body.vendor_id,
            bidder.bid_id,
        )
        idempotent = _idempotent_operation(session, principal.tenant_id, idempotency_key, request_hash)
        if idempotent:
            evaluation = session.scalar(
                select(Evaluation).where(Evaluation.id == idempotent.entity_id).with_for_update()
            )
            if not evaluation:
                raise HTTPException(status_code=409, detail="idempotent evaluation job is inconsistent")
            return _accepted(request, idempotent, project_id=project.id, bid_id=bidder.bid_id, vendor_id=bidder.vendor_key, evaluation_id=evaluation.id)
        if bidder.classification_status != "approved":
            raise HTTPException(status_code=409, detail="approve bidder classification before evaluation")
        version = session.get(TenderVersion, project.tender_version_id)
        understanding = dict(version.understanding_json or {}) if version else {}
        if not version or version.status != "approved" or not (understanding.get("frozen_contract") or {}).get("criteria"):
            raise HTTPException(status_code=409, detail="freeze the reviewed tender contract before evaluation")
        active_evaluation = session.scalar(
            select(Evaluation)
            .where(
                Evaluation.bid_id == bidder.bid_id,
                Evaluation.status.in_(("queued", "running")),
            )
            .order_by(Evaluation.created_at.desc())
            .with_for_update()
        )
        if active_evaluation:
            operation = session.scalar(
                select(Operation)
                .where(
                    Operation.entity_type == "evaluation",
                    Operation.entity_id == active_evaluation.id,
                    Operation.status.in_(("queued", "running")),
                )
                .order_by(Operation.created_at.desc())
                .with_for_update()
            )
            if not operation:
                raise HTTPException(status_code=409, detail="active evaluation is missing its operation")
            return _accepted(
                request,
                operation,
                project_id=project.id,
                bid_id=bidder.bid_id,
                vendor_id=bidder.vendor_key,
                evaluation_id=active_evaluation.id,
            )
        if bidder.evaluation_id:
            existing = session.scalar(
                select(Evaluation).where(Evaluation.id == bidder.evaluation_id).with_for_update()
            )
            operation = session.scalar(
                select(Operation)
                .where(Operation.entity_type == "evaluation", Operation.entity_id == bidder.evaluation_id)
                .order_by(Operation.created_at.desc())
                .with_for_update()
            )
            if existing and operation and existing.status in {"queued", "running", "succeeded"}:
                if idempotency_key:
                    session.add(IdempotencyKey(
                        id=new_id(),
                        tenant_id=principal.tenant_id,
                        key=idempotency_key,
                        request_hash=request_hash,
                        operation_id=operation.id,
                    ))
                return _accepted(request, operation, project_id=project.id, bid_id=bidder.bid_id, vendor_id=bidder.vendor_key, evaluation_id=existing.id)
        evaluation = Evaluation(
            id=new_id(),
            tenant_id=principal.tenant_id,
            tender_version_id=project.tender_version_id,
            bid_id=bidder.bid_id,
            status="queued",
        )
        session.add(evaluation)
        session.flush([evaluation])
        try:
            operation = enqueue(
                session,
                tenant_id=principal.tenant_id,
                entity_type="evaluation",
                entity_id=evaluation.id,
                stage="evaluation",
                payload={
                    "evaluation_id": evaluation.id,
                    "tender_version_id": project.tender_version_id,
                    "bid_id": bidder.bid_id,
                },
                key=idempotency_key,
                request_hash=request_hash,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        bidder.evaluation_id = evaluation.id
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="pipeline.evaluation.queued",
            entity_type="evaluation",
            entity_id=evaluation.id,
            reason=None,
            before=None,
            after={"operation_id": operation.id, "vendor_id": bidder.vendor_key},
        )
        return _accepted(request, operation, project_id=project.id, bid_id=bidder.bid_id, vendor_id=bidder.vendor_key, evaluation_id=evaluation.id)


@router.get("/jobs/{job_id}", response_model=PipelineJob)
def pipeline_job(job_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        operation = _owned(session, Operation, job_id, principal.tenant_id)
        if operation.stage not in {"understanding", "intake", "evaluation"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline job not found.")
        result: PipelineResult | None = None
        result_type: str | None = None
        if operation.status == "succeeded" and operation.stage == "understanding":
            project = session.get(WorkbenchProject, operation.entity_id)
            pinned_version_id = str((operation.payload_json or {}).get("tender_version_id") or "")
            if project and pinned_version_id and pinned_version_id != project.tender_version_id:
                raise HTTPException(status_code=409, detail="job result belongs to a superseded tender version")
            version = session.get(TenderVersion, pinned_version_id or project.tender_version_id) if project else None
            if project and version:
                result = TenderUnderstandingResult.model_validate(
                    _tender_result(session, project, version, request)
                )
                result_type = "tender_understanding"
        elif operation.status == "succeeded" and operation.stage == "intake":
            project = session.get(WorkbenchProject, operation.entity_id)
            vendor_key = str((operation.payload_json or {}).get("vendor_key") or "")
            bidder = session.scalar(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_key)) if project and vendor_key else None
            pinned_bid_id = str((operation.payload_json or {}).get("bid_id") or "")
            if bidder and pinned_bid_id and pinned_bid_id != bidder.bid_id:
                raise HTTPException(status_code=409, detail="job result belongs to a superseded bidder package")
            if project and bidder:
                result = BidClassificationResult.model_validate(
                    _classification_result(session, project, bidder, request)
                )
                result_type = "bid_classification"
        elif operation.status == "succeeded" and operation.stage == "evaluation":
            evaluation = session.get(Evaluation, operation.entity_id)
            bidder = session.scalar(
                select(WorkbenchBid).where(WorkbenchBid.evaluation_id == operation.entity_id)
            )
            project = session.get(WorkbenchProject, bidder.project_id) if bidder else None
            if evaluation and not bidder:
                raise HTTPException(status_code=409, detail="job result belongs to a superseded bidder package")
            if evaluation and bidder and project:
                base_url = _base_url(request)
                result = BidEvaluationResult(
                    project_id=project.id,
                    bid_id=bidder.bid_id,
                    vendor_id=bidder.vendor_key,
                    evaluation_id=evaluation.id,
                    evaluation=PublicEvaluationResult.model_validate(evaluation.result_json),
                    links=PipelineLinks(
                        evaluation_results=f"{base_url}/v1/workbench/projects/{project.id}/results",
                        report=f"{base_url}/v1/workbench/projects/{project.id}/report",
                    ),
                )
                result_type = "bid_evaluation"
        return {
            "job_id": operation.id,
            "stage": operation.stage,
            "status": "complete" if operation.status == "succeeded" else operation.status,
            "result_type": result_type,
            "error_code": operation.error_code,
            "error": operation.error_detail,
            "result": result,
        }
