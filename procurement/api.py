"""Production resource APIs. The legacy /evaluate façade remains in app.py."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from procurement.domain.models import Bid, Document, Evaluation, IdempotencyKey, Operation, Review, Tender, Tenant, TenderVersion, User, WorkbenchBid, new_id
from procurement.platform.auth import require_role
from procurement.platform.database import make_session_factory, on_rollback, session_scope
from procurement.platform.env import env_int
from procurement.platform.ingestion import (
    IngestionValidationError,
    safe_filename,
    validate_document_container,
)
from procurement.platform.operations import IdempotencyConflict, audit, enqueue, idempotent_result, remember_result
from procurement.platform.object_store import DiskCapacityError, delete_file, ensure_free_disk, put_file

router = APIRouter(prefix="/v1")
_UPLOAD_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".zip"}
BID_UPLOAD_SUFFIXES = frozenset({".pdf", ".zip"})


@lru_cache(maxsize=1)
def _sessions():
    try:
        return make_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL is not configured") from exc


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _required_idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if len(key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return key


def _upload_sha256(upload: UploadFile) -> str:
    position = upload.file.tell()
    digest = hashlib.sha256()
    total = 0
    maximum = env_int("MAX_UPLOAD_MB", 50, minimum=1, maximum=50) * 1024 * 1024
    try:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HTTPException(status_code=413, detail="upload exceeds configured maximum")
            digest.update(chunk)
    finally:
        upload.file.seek(position)
    return digest.hexdigest()


def _owned(session: Any, model: Any, identifier: str, tenant_id: str) -> Any:
    entity = session.scalar(select(model).where(model.id == identifier, model.tenant_id == tenant_id))
    if not entity:
        raise HTTPException(status_code=404, detail="resource not found")
    return entity


def _ensure_bid_capacity(session: Any, tender_version_id: str, tenant_id: str) -> TenderVersion:
    version = session.scalar(
        select(TenderVersion)
        .join(Tender, Tender.id == TenderVersion.tender_id)
        .where(TenderVersion.id == tender_version_id, Tender.tenant_id == tenant_id)
        .with_for_update()
    )
    if not version:
        raise HTTPException(status_code=404, detail="tender version not found")
    maximum = env_int("MAX_BIDDERS_PER_TENDER", 10, minimum=1, maximum=10)
    current = session.scalar(
        select(func.count(Bid.id)).where(
            Bid.tender_version_id == version.id,
            Bid.status.notin_(("superseded", "withdrawn")),
        )
    ) or 0
    if current >= maximum:
        raise HTTPException(status_code=409, detail=f"a tender can contain at most {maximum} bidders")
    return version


def _actor_user_id(session: Any, principal: Any) -> str:
    if not session.get(Tenant, principal.tenant_id):
        raise HTTPException(status_code=403, detail="tenant is not provisioned")
    user = session.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.subject == principal.subject))
    if not user:
        role = next((name for name in ("admin", "approver", "procurement_officer", "reviewer", "auditor") if name in principal.roles), "reviewer")
        user = User(id=new_id(), tenant_id=principal.tenant_id, subject=principal.subject, role=role)
        session.add(user)
    return user.id


class TenderCreate(BaseModel):
    external_reference: str | None = Field(default=None, max_length=255)


class TenderVersionCreate(BaseModel):
    understanding: dict[str, Any] = Field(default_factory=dict)


class BidCreate(BaseModel):
    tender_version_id: str
    bidder_name: str | None = Field(default=None, max_length=512)


class EvaluationCreate(BaseModel):
    bid_id: str


class ReviewCreate(BaseModel):
    action: str = Field(pattern="^(approve|reject|return_for_clarification)$")
    reason: str = Field(min_length=3, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)


def _safe_filename(value: str) -> str:
    try:
        return safe_filename(value)
    except IngestionValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _validate_document_container(path: Path, suffix: str) -> None:
    try:
        validate_document_container(
            path,
            suffix,
            allowed_zip_suffixes=_UPLOAD_SUFFIXES - {".zip"},
        )
    except IngestionValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _store_document(
    session: Any,
    upload: UploadFile,
    *,
    tenant_id: str,
    actor_id: str,
    tender_id: str | None,
    tender_version_id: str | None = None,
    bid_id: str | None,
    allowed_suffixes: set[str] | frozenset[str] | None = None,
    source_kind: str = "upload",
) -> Document:
    filename = _safe_filename(upload.filename or "upload.bin")
    suffix = os.path.splitext(filename)[1].lower()
    accepted_suffixes = _UPLOAD_SUFFIXES if allowed_suffixes is None else allowed_suffixes
    if suffix not in accepted_suffixes:
        raise HTTPException(status_code=415, detail="bid upload must be a single PDF or ZIP file")
    maximum = env_int("MAX_UPLOAD_MB", 50, minimum=1, maximum=50) * 1024 * 1024
    try:
        ensure_free_disk(Path(tempfile.gettempdir()), additional_bytes=maximum)
    except DiskCapacityError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    work = Path(tempfile.mkdtemp(prefix="procurement-upload-"))
    path = work / filename
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise HTTPException(status_code=413, detail="upload exceeds configured maximum")
                digest.update(chunk)
                handle.write(chunk)
        if not total:
            raise HTTPException(status_code=400, detail="upload is empty")
        _validate_document_container(path, suffix)
        checksum = digest.hexdigest()
        document_id = new_id()
        prefix = os.getenv("OBJECT_STORAGE_PREFIX", "procurement").strip().strip("/")
        if not prefix or PurePosixPath(prefix).is_absolute() or ".." in PurePosixPath(prefix).parts:
            raise HTTPException(status_code=503, detail="OBJECT_STORAGE_PREFIX is invalid")
        key = f"{prefix}/{tenant_id}/documents/{checksum}/{document_id}/{filename}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        scope_column = (
            Document.tender_version_id
            if tender_version_id
            else Document.bid_id
            if bid_id
            else Document.tender_id
        )
        scope_value = tender_version_id or bid_id or tender_id
        if scope_column is not None and scope_value:
            existing = session.scalar(
                select(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.sha256 == checksum,
                    scope_column == scope_value,
                    Document.source_kind != "superseded_upload",
                )
                .order_by(Document.created_at, Document.id)
                .limit(1)
            )
            if existing:
                # ponytail: uploads are single-session today; add a database uniqueness
                # constraint if concurrent uploads are enabled later.
                audit(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_id,
                    event_type="document.duplicate_reused",
                    entity_type="document",
                    entity_id=existing.id,
                    reason="same_content_sha256",
                    before=None,
                    after={
                        "sha256": checksum,
                        "requested_file_name": filename,
                        "reused_document_id": existing.id,
                        "tender_id": tender_id,
                        "tender_version_id": tender_version_id,
                        "bid_id": bid_id,
                    },
                )
                return existing
        try:
            put_file(path, key, content_type=content_type)
        except DiskCapacityError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        on_rollback(session, lambda: delete_file(key))
        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            tender_id=tender_id,
            tender_version_id=tender_version_id,
            bid_id=bid_id,
            sha256=checksum,
            object_key=key,
            file_name=filename,
            content_type=content_type,
            size_bytes=total,
            source_kind=source_kind,
            extraction_status="stored",
        )
        session.add(document)
        audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            event_type="document.stored",
            entity_type="document",
            entity_id=document.id,
            reason=None,
            before=None,
            after={
                "file_name": filename,
                "content_type": content_type,
                "sha256": checksum,
                "size_bytes": total,
                "tender_id": tender_id,
                "tender_version_id": tender_version_id,
                "bid_id": bid_id,
            },
        )
        return document
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/tenders", status_code=status.HTTP_201_CREATED)
def create_tender(body: TenderCreate, request: Request) -> dict[str, str]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "tender.create", **body.model_dump(mode="json")})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        tender = Tender(id=new_id(), tenant_id=principal.tenant_id, external_reference=body.external_reference, status="draft")
        session.add(tender)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="tender.created", entity_type="tender", entity_id=tender.id, reason=None, before=None, after={"status": tender.status})
        result = {"tender_id": tender.id, "status": tender.status}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="tender", entity_id=tender.id, stage="tender_create", result=result)
        return result


@router.post("/tenders/{tender_id}/versions", status_code=status.HTTP_201_CREATED)
def create_tender_version(tender_id: str, body: TenderVersionCreate, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "tender.version.create", "tender_id": tender_id, **body.model_dump(mode="json")})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        tender = _owned(session, Tender, tender_id, principal.tenant_id)
        session.refresh(tender, with_for_update=True)
        latest = session.scalar(
            select(TenderVersion)
            .where(TenderVersion.tender_id == tender_id)
            .order_by(TenderVersion.version_number.desc())
            .limit(1)
            .with_for_update()
        )
        version = TenderVersion(
            id=new_id(),
            tender_id=tender_id,
            previous_version_id=latest.id if latest else None,
            version_number=int(latest.version_number if latest else 0) + 1,
            status="draft",
            understanding_json=body.understanding,
        )
        session.add(version)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="tender.version.created", entity_type="tender_version", entity_id=version.id, reason=None, before=None, after={"version_number": version.version_number})
        result = {"tender_version_id": version.id, "version_number": version.version_number, "status": version.status}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="tender_version", entity_id=version.id, stage="tender_version_create", result=result)
        return result


@router.post("/bids", status_code=status.HTTP_201_CREATED)
def create_bid(body: BidCreate, request: Request) -> dict[str, str]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "bid.create", **body.model_dump(mode="json")})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        version = _ensure_bid_capacity(session, body.tender_version_id, principal.tenant_id)
        if version.status != "approved" or not ((version.understanding_json or {}).get("frozen_contract") or {}).get("criteria"):
            raise HTTPException(status_code=409, detail="finalize the evaluation checklist before adding bids")
        bid = Bid(id=new_id(), tenant_id=principal.tenant_id, tender_version_id=version.id, bidder_name=body.bidder_name, status="uploaded")
        session.add(bid)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="bid.created", entity_type="bid", entity_id=bid.id, reason=None, before=None, after={"status": bid.status})
        result = {"bid_id": bid.id, "status": bid.status}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="bid", entity_id=bid.id, stage="bid_create", result=result)
        return result


@router.post("/tender-versions/{tender_version_id}/approve")
def approve_tender_version(tender_version_id: str, request: Request) -> dict[str, str]:
    principal = require_role(request, "admin", "approver")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        version = session.get(TenderVersion, tender_version_id)
        if not version or not _owned(session, Tender, version.tender_id, principal.tenant_id):
            raise HTTPException(status_code=404, detail="tender version not found")
        if version.status != "draft":
            raise HTTPException(status_code=409, detail="tender version is already reviewed")
        version.status = "approved"
        version.approved_by = actor_id
        version.approved_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="tender.version.approved", entity_type="tender_version", entity_id=version.id, reason=None, before={"status": "draft"}, after={"status": "approved"})
        return {"tender_version_id": version.id, "status": version.status}


@router.post("/tenders/{tender_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_tender_document(tender_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, str]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "tender.document.upload", "tender_id": tender_id, "file_sha256": _upload_sha256(file)})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        _owned(session, Tender, tender_id, principal.tenant_id)
        version = session.scalar(
            select(TenderVersion)
            .where(TenderVersion.tender_id == tender_id)
            .order_by(TenderVersion.version_number.desc())
            .limit(1)
        )
        if not version:
            raise HTTPException(status_code=409, detail="create a tender version before uploading documents")
        document = await _store_document(
            session,
            file,
            tenant_id=principal.tenant_id,
            actor_id=actor_id,
            tender_id=tender_id,
            tender_version_id=version.id,
            bid_id=None,
        )
        result = {"document_id": document.id, "status": document.extraction_status}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="document", entity_id=document.id, stage="tender_document_upload", result=result)
        return result


@router.post("/bids/{bid_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_bid_document(bid_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, str]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "bid.document.upload", "bid_id": bid_id, "file_sha256": _upload_sha256(file)})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        _owned(session, Bid, bid_id, principal.tenant_id)
        from procurement.bhel_evaluation import load_bhel_profile

        profile = load_bhel_profile()
        policy = profile.get("submission_policy") if isinstance(profile, dict) else {}
        if isinstance(policy, dict) and policy.get("additional_documents_allowed") is False:
            workbench_bid = session.scalar(select(WorkbenchBid).where(WorkbenchBid.bid_id == bid_id))
            evaluation_exists = session.scalar(select(Evaluation.id).where(Evaluation.bid_id == bid_id).limit(1))
            if (workbench_bid and workbench_bid.classification_status == "approved") or evaluation_exists:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "The BHEL bidder submission is frozen after classification approval; additional documents are not accepted.",
                        "code": "BHEL_SUBMISSION_FROZEN",
                    },
                )
        document = await _store_document(session, file, tenant_id=principal.tenant_id, actor_id=actor_id, tender_id=None, bid_id=bid_id)
        result = {"document_id": document.id, "status": document.extraction_status}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="document", entity_id=document.id, stage="bid_document_upload", result=result)
        return result


@router.post("/evaluations", status_code=status.HTTP_202_ACCEPTED)
def start_evaluation(body: EvaluationCreate, request: Request) -> dict[str, str]:
    principal = require_role(request, "admin", "procurement_officer")
    key = _required_idempotency_key(request)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        bid = _owned(session, Bid, body.bid_id, principal.tenant_id)
        version = session.get(TenderVersion, bid.tender_version_id)
        tender = session.get(Tender, version.tender_id) if version else None
        if not version or not tender or version.status != "approved":
            raise HTTPException(status_code=409, detail="an approved tender version is required before evaluation")
        if not session.scalar(select(Document.id).where(
            Document.tender_version_id == version.id,
            Document.tenant_id == principal.tenant_id,
        ).limit(1)):
            raise HTTPException(status_code=409, detail="tender documents are required before evaluation")
        if not session.scalar(select(Document.id).where(Document.bid_id == bid.id, Document.tenant_id == principal.tenant_id).limit(1)):
            raise HTTPException(status_code=409, detail="bid documents are required before evaluation")
        workbench_bid = session.scalar(select(WorkbenchBid).where(WorkbenchBid.bid_id == bid.id))
        if not workbench_bid or workbench_bid.classification_status != "approved":
            raise HTTPException(status_code=409, detail="approved bidder classification is required before evaluation")
        if not (dict(version.understanding_json or {}).get("frozen_contract") or {}).get("criteria"):
            raise HTTPException(status_code=409, detail="frozen reviewed tender contract is required before evaluation")
        request_hash = _hash({"bid_id": bid.id})
        if key:
            existing = session.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == principal.tenant_id, IdempotencyKey.key == key))
            if existing:
                if existing.request_hash != request_hash:
                    raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different request")
                operation = session.get(Operation, existing.operation_id)
                if not operation:
                    raise HTTPException(status_code=409, detail="idempotency record is inconsistent")
                return {"evaluation_id": operation.entity_id, "operation_id": operation.id, "status": operation.status}
        evaluation = Evaluation(id=new_id(), tenant_id=principal.tenant_id, tender_version_id=bid.tender_version_id, bid_id=bid.id, status="queued")
        session.add(evaluation)
        payload = {
            "evaluation_id": evaluation.id,
            "tender_version_id": bid.tender_version_id,
            "bid_id": bid.id,
        }
        try:
            operation = enqueue(session, tenant_id=principal.tenant_id, entity_type="evaluation", entity_id=evaluation.id, stage="evaluation", payload=payload, key=key, request_hash=request_hash)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="evaluation.queued", entity_type="evaluation", entity_id=evaluation.id, reason=None, before=None, after={"operation_id": operation.id})
        return {"evaluation_id": evaluation.id, "operation_id": operation.id, "status": operation.status}


@router.get("/operations/{operation_id}")
def get_operation(operation_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        operation = _owned(session, Operation, operation_id, principal.tenant_id)
        retry_after_seconds = None
        if operation.status == "queued" and operation.available_at:
            retry_after_seconds = max(
                0,
                int((operation.available_at - datetime.now(timezone.utc)).total_seconds()),
            )
        result = dict(operation.result_json or {})
        return {
            "operation_id": operation.id,
            "entity_id": operation.entity_id,
            "stage": operation.stage,
            "status": operation.status,
            "error_code": operation.error_code,
            "error": operation.error_detail,
            "error_detail": operation.error_detail,
            "retry_after_seconds": retry_after_seconds,
            "progress": result.get("progress"),
            "checkpoint": result.get("checkpoint"),
            "attempt": operation.attempt,
            "state_version": operation.state_version,
            "progress_version": operation.progress_version,
            "created_at": operation.created_at.isoformat() if operation.created_at else None,
            "started_at": operation.started_at.isoformat() if operation.started_at else None,
            "finished_at": operation.finished_at.isoformat() if operation.finished_at else None,
            "result": operation.result_json,
        }


@router.post("/evaluations/{evaluation_id}/reviews", status_code=status.HTTP_201_CREATED)
def create_review(evaluation_id: str, body: ReviewCreate, request: Request) -> dict[str, str]:
    principal = require_role(request, "admin", "approver")
    key = _required_idempotency_key(request)
    request_hash = _hash({"action": "evaluation.review", "evaluation_id": evaluation_id, **body.model_dump(mode="json")})
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        _owned(session, Evaluation, evaluation_id, principal.tenant_id)
        review = Review(id=new_id(), evaluation_id=evaluation_id, reviewer_user_id=actor_id, action=body.action, reason=body.reason, payload_json=body.payload)
        session.add(review)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type=f"evaluation.review.{body.action}", entity_type="evaluation", entity_id=evaluation_id, reason=body.reason, before=None, after=body.payload)
        result = {"review_id": review.id, "status": "recorded"}
        remember_result(session, tenant_id=principal.tenant_id, key=key, request_hash=request_hash, entity_type="review", entity_id=review.id, stage="evaluation_review", result=result)
        return result


@router.get("/reports/{evaluation_id}")
def get_report(evaluation_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        evaluation = _owned(session, Evaluation, evaluation_id, principal.tenant_id)
        if evaluation.status != "succeeded" or not evaluation.result_json:
            raise HTTPException(status_code=409, detail="evaluation report is not ready")
        return evaluation.result_json
