"""Bidder intake and document-classification routes."""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from procurement.api import BID_UPLOAD_SUFFIXES, _actor_user_id, _ensure_bid_capacity, _hash, _sessions, _store_document, _upload_sha256
from procurement.domain.models import Bid, Clarification, Document, Evaluation, Operation, TenderVersion, WorkbenchBid, WorkbenchProject, new_id
from procurement.domain.revisions import supersede_bid
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.object_store import download_file
from procurement.platform.operations import IdempotencyConflict, audit, enqueue, idempotent_result, remember_result
from procurement.workbench.common import ClassificationApproval, ClassificationCorrection, _PHYSICAL_ROUTES, _bidder, _classification_payload, _document_item, _idempotency_key, _key, _normalize_physical_route, _preview_delivery, _preview_is_pdf, _project, _render_pdf_page, _state, _unlink_many, sync_bidder_search_manifest

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])
logger = logging.getLogger("procurement.workbench.bidders")


def _require_frozen_checklist(session: Any, project: WorkbenchProject) -> TenderVersion:
    version = session.get(TenderVersion, project.tender_version_id)
    frozen = (version.understanding_json or {}).get("frozen_contract") if version else None
    if not version or version.status != "approved" or not (frozen or {}).get("criteria"):
        raise HTTPException(status_code=409, detail="finalize the evaluation checklist before processing bids")
    return version


def _locked_project(session: Any, project_id: str, tenant_id: str) -> WorkbenchProject:
    project = session.scalar(
        select(WorkbenchProject)
        .where(WorkbenchProject.id == project_id, WorkbenchProject.tenant_id == tenant_id)
        .with_for_update()
    )
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _locked_bidder(session: Any, project: WorkbenchProject, vendor_key: str) -> WorkbenchBid:
    row = session.scalar(
        select(WorkbenchBid)
        .where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_key)
        .with_for_update()
    )
    if not row:
        raise HTTPException(status_code=404, detail="bidder not found")
    return row


def _active_classification_operation(session: Any, project_id: str, vendor_key: str) -> Operation | None:
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


def _duplicate_bid_vendor(session: Any, project_id: str, file_sha256: str) -> str | None:
    return session.scalar(
        select(WorkbenchBid.vendor_key)
        .join(Document, Document.bid_id == WorkbenchBid.bid_id)
        .where(WorkbenchBid.project_id == project_id, Document.sha256 == file_sha256)
        .limit(1)
    )


@router.post("/projects/{project_id}/bids", status_code=status.HTTP_201_CREATED)
async def add_bid(
    project_id: str,
    request: Request,
    vendor_name: str | None = Form(None),
    vendor_label: str | None = Form(None),
    received_at: str | None = Form(None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    vendor_name = (vendor_name or vendor_label or "").strip()
    if not vendor_name:
        raise HTTPException(status_code=422, detail="vendor_name is required")
    vendor_key = _key(vendor_name)
    idempotency_key = _idempotency_key(request, required=True)
    file_sha256 = _upload_sha256(file)
    request_hash = _hash({
        "action": "workbench.bid.add",
        "project_id": project_id,
        "vendor_key": vendor_key,
        "received_at": received_at,
        "file_sha256": file_sha256,
    })
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        project = _locked_project(session, project_id, principal.tenant_id)
        version = _require_frozen_checklist(session, project)
        if session.scalar(select(WorkbenchBid.id).where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_key)):
            raise HTTPException(status_code=409, detail="bidder already exists in this project")
        duplicate_vendor = _duplicate_bid_vendor(session, project.id, file_sha256)
        if duplicate_vendor:
            raise HTTPException(
                status_code=409,
                detail={"code": "DUPLICATE_BID_PACKAGE", "message": f"This bidder package is already uploaded for {duplicate_vendor}."},
            )
        _ensure_bid_capacity(session, version.id, principal.tenant_id)
        bid = Bid(id=new_id(), tenant_id=principal.tenant_id, tender_version_id=version.id, bidder_name=vendor_name.strip(), status="uploaded")
        session.add(bid)
        session.flush([bid])
        document = await _store_document(
            session,
            file,
            tenant_id=principal.tenant_id,
            actor_id=actor_id,
            tender_id=None,
            bid_id=bid.id,
            allowed_suffixes=BID_UPLOAD_SUFFIXES,
        )
        workbench_bid = WorkbenchBid(id=new_id(), project_id=project.id, bid_id=bid.id, vendor_key=vendor_key, vendor_name=vendor_name.strip())
        session.add(workbench_bid)
        project.updated_at = datetime.now(timezone.utc)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="workbench.bid.added", entity_type="workbench_bid", entity_id=workbench_bid.id, reason=None, before=None, after={"bid_id": bid.id, "document_id": document.id})
        session.flush()
        result = {
            "vendor_id": vendor_key,
            "bid": {"vendor_id": workbench_bid.id, "vendor_key": vendor_key, "bid_id": bid.id},
            "state": _state(session, project),
        }
        remember_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            entity_type="workbench_bid",
            entity_id=workbench_bid.id,
            stage="bid_add",
            result=result,
        )
        return result


@router.post("/projects/{project_id}/bids/{vendor_key}/replace", status_code=status.HTTP_201_CREATED)
async def replace_bid(
    project_id: str,
    vendor_key: str,
    request: Request,
    file: UploadFile = File(...),
    reason: str = Form(...),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="a replacement reason is required")
    idempotency_key = _idempotency_key(request, required=True)
    file_sha256 = _upload_sha256(file)
    request_hash = _hash({
        "action": "workbench.bid.replace",
        "project_id": project_id,
        "vendor_key": vendor_key,
        "reason": reason,
        "file_sha256": file_sha256,
    })
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        project = _locked_project(session, project_id, principal.tenant_id)
        _require_frozen_checklist(session, project)
        row = _locked_bidder(session, project, vendor_key)
        duplicate_vendor = _duplicate_bid_vendor(session, project.id, file_sha256)
        if duplicate_vendor:
            raise HTTPException(
                status_code=409,
                detail={"code": "DUPLICATE_BID_PACKAGE", "message": f"This bidder package is already uploaded for {duplicate_vendor}."},
            )
        if _active_classification_operation(session, project.id, row.vendor_key):
            raise HTTPException(status_code=409, detail="wait for bidder classification before replacing this package")
        evaluation_ids = select(Evaluation.id).where(
            Evaluation.bid_id == row.bid_id,
            Evaluation.status.in_(("queued", "running")),
        )
        if session.scalar(
            select(Operation.id).where(
                Operation.tenant_id == principal.tenant_id,
                Operation.entity_type == "evaluation",
                Operation.entity_id.in_(evaluation_ids),
                Operation.status.in_(("queued", "running")),
            ).limit(1)
        ):
            raise HTTPException(status_code=409, detail="wait for bidder evaluation before replacing this package")
        current = session.scalar(
            select(Bid)
            .where(Bid.id == row.bid_id, Bid.tenant_id == principal.tenant_id)
            .with_for_update()
        )
        if not current:
            raise HTTPException(status_code=409, detail="current bidder revision is missing")
        before = {
            "bid_id": current.id,
            "revision_number": current.revision_number,
            "classification_status": row.classification_status,
            "evaluation_id": row.evaluation_id,
        }
        replacement = supersede_bid(
            current,
            snapshot={
                "reason": reason,
                "classification_status": row.classification_status,
                "intake": dict(row.intake_json or {}),
                "evaluation_id": row.evaluation_id,
            },
        )
        session.add(replacement)
        session.flush([replacement])
        current.revision_json = {
            **dict(current.revision_json or {}),
            "superseded_by_bid_id": replacement.id,
        }
        document = await _store_document(
            session,
            file,
            tenant_id=principal.tenant_id,
            actor_id=actor_id,
            tender_id=None,
            bid_id=replacement.id,
            allowed_suffixes=BID_UPLOAD_SUFFIXES,
        )
        for clarification in session.scalars(
            select(Clarification).where(
                Clarification.workbench_bid_id == row.id,
                Clarification.status.in_(("draft", "issued", "responded")),
            )
        ):
            clarification.status = "superseded"
        row.bid_id = replacement.id
        row.classification_status = "pending"
        row.intake_json = {}
        row.evaluation_id = None
        project.award_json = None
        if project.stage in {"bids_intaken", "evaluated", "report_generated"}:
            project.stage = "contract_frozen"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.bid.replaced",
            entity_type="workbench_bid",
            entity_id=row.id,
            reason=reason,
            before=before,
            after={
                "bid_id": replacement.id,
                "revision_number": replacement.revision_number,
                "document_id": document.id,
                "classification_status": "pending",
                "evaluation_id": None,
            },
        )
        result = {
            "vendor_id": row.vendor_key,
            "bid_id": replacement.id,
            "revision_number": replacement.revision_number,
            "state": _state(session, project),
        }
        remember_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            entity_type="workbench_bid",
            entity_id=row.id,
            stage="bid_replace",
            result=result,
        )
        return result


@router.get("/projects/{project_id}/bids/{vendor_key}/revisions")
def bid_revisions(project_id: str, vendor_key: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        row = _bidder(session, project, vendor_key)
        revisions: list[dict[str, Any]] = []
        bid_id: str | None = row.bid_id
        seen: set[str] = set()
        while bid_id and bid_id not in seen:
            seen.add(bid_id)
            bid = session.scalar(
                select(Bid).where(Bid.id == bid_id, Bid.tenant_id == principal.tenant_id)
            )
            if not bid:
                break
            documents = list(
                session.scalars(
                    select(Document)
                    .where(Document.bid_id == bid.id, Document.tenant_id == principal.tenant_id)
                    .order_by(Document.created_at, Document.id)
                )
            )
            current = bid.id == row.bid_id
            snapshot = dict(bid.revision_json or {})
            revisions.append(
                {
                    "bid_id": bid.id,
                    "revision_number": bid.revision_number,
                    "status": bid.status,
                    "current": current,
                    "classification_status": row.classification_status if current else snapshot.get("classification_status"),
                    "evaluation_id": row.evaluation_id if current else snapshot.get("evaluation_id"),
                    "reason": snapshot.get("reason"),
                    "documents": [
                        {
                            "document_id": document.id,
                            "file_name": document.file_name,
                            "sha256": document.sha256,
                            "size_bytes": document.size_bytes,
                        }
                        for document in documents
                    ],
                    "created_at": bid.created_at.isoformat() if bid.created_at else None,
                    "superseded_at": bid.superseded_at.isoformat() if bid.superseded_at else None,
                }
            )
            bid_id = bid.previous_bid_id
        return {"vendor_id": row.vendor_key, "revisions": revisions}


@router.delete("/projects/{project_id}/bids/{vendor_key}")
def delete_bid(project_id: str, vendor_key: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    idempotency_key = _idempotency_key(request, required=True)
    request_hash = _hash({
        "action": "workbench.bid.withdraw",
        "project_id": project_id,
        "vendor_key": vendor_key,
    })
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        replay = idempotent_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        project = _locked_project(session, project_id, principal.tenant_id)
        row = _locked_bidder(session, project, vendor_key)
        active_intake = session.scalar(
            select(Operation.id).where(
                Operation.tenant_id == principal.tenant_id,
                Operation.entity_type == "workbench_project",
                Operation.entity_id == project.id,
                Operation.stage == "intake",
                Operation.status.in_(("queued", "running")),
            )
        )
        if active_intake:
            raise HTTPException(status_code=409, detail="stop the active bidder intake before deleting a bidder")
        if row.classification_status == "approved":
            raise HTTPException(status_code=409, detail="approved bidder classification is immutable")
        if row.evaluation_id or session.scalar(select(Evaluation.id).where(Evaluation.bid_id == row.bid_id)):
            raise HTTPException(status_code=409, detail="evaluated bidders cannot be deleted")
        if session.scalar(select(Clarification.id).where(Clarification.workbench_bid_id == row.id)):
            raise HTTPException(status_code=409, detail="bidders with clarification history cannot be deleted")
        bid = session.get(Bid, row.bid_id)
        documents = list(
            session.scalars(
                select(Document).where(
                    Document.bid_id == row.bid_id,
                    Document.tenant_id == principal.tenant_id,
                )
            )
        )
        object_keys = [document.object_key for document in documents]
        object_keys.extend(
            str(source.get("object_key"))
            for source in (row.intake_json or {}).get("preview_sources", {}).values()
            if isinstance(source, dict) and source.get("object_key")
        )
        withdrawal = {
            "reason": "bidder removed before approval",
            "workbench_bid_id": row.id,
            "vendor_key": row.vendor_key,
            "vendor_name": row.vendor_name,
            "document_ids": [document.id for document in documents],
            "object_keys": object_keys,
            "classification_status": row.classification_status,
            "intake": dict(row.intake_json or {}),
        }
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.bid.withdrawn",
            entity_type="workbench_bid",
            entity_id=row.id,
            reason="bidder removed before approval",
            before={
                "bid_id": row.bid_id,
                "vendor_key": row.vendor_key,
                "vendor_name": row.vendor_name,
                "document_ids": [document.id for document in documents],
                "object_keys": object_keys,
            },
            after={"bid_status": "withdrawn", "data_retained": True},
        )
        if bid:
            bid.status = "withdrawn"
            bid.revision_json = {**dict(bid.revision_json or {}), "withdrawal": withdrawal}
        session.delete(row)
        project.updated_at = datetime.now(timezone.utc)
        session.flush()
        result = {"status": "withdrawn", "vendor_id": vendor_key, "state": _state(session, project)}
        remember_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            entity_type="bid",
            entity_id=bid.id if bid else row.bid_id,
            stage="bid_withdraw",
            result=result,
        )
        return result


@router.post("/projects/{project_id}/intake", status_code=status.HTTP_202_ACCEPTED)
def intake_project(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _locked_project(session, project_id, principal.tenant_id)
        _require_frozen_checklist(session, project)
        bidders = list(session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id)))
        if not bidders:
            raise HTTPException(status_code=409, detail="at least one bidder package is required")
        existing = session.scalar(
            select(Operation)
            .where(
                Operation.tenant_id == principal.tenant_id,
                Operation.entity_type == "workbench_project",
                Operation.entity_id == project.id,
                Operation.stage == "intake",
                Operation.status.in_(("queued", "running")),
            )
            .order_by(Operation.created_at.desc())
        )
        if existing:
            return {"job_id": existing.id, "status": existing.status, "state": _state(session, project)}
        operation = enqueue(
            session,
            tenant_id=principal.tenant_id,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="intake",
            payload={
                "project_id": project.id,
                "tender_version_id": project.tender_version_id,
                "bid_ids": [row.bid_id for row in bidders],
            },
            key=_idempotency_key(request, required=True),
            request_hash=(
                f"{project.id}:{project.tender_version_id}:"
                + ",".join(sorted(row.bid_id for row in bidders))
            ),
        )
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.intake.queued",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"stage": project.stage},
            after={"operation_id": operation.id},
        )
        return {"job_id": operation.id, "status": operation.status, "state": _state(session, project)}


@router.get("/projects/{project_id}/bids/{vendor_key}/classification")
def get_classification(project_id: str, vendor_key: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        row = _bidder(session, project, vendor_key)
        if not row.intake_json:
            raise HTTPException(status_code=409, detail="bidder intake is not complete")
        return _classification_payload(row)


@router.get("/projects/{project_id}/bids/{vendor_key}/review")
def get_bid_review(project_id: str, vendor_key: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        row = _bidder(session, project, vendor_key)
        intake = dict(row.intake_json or {})
        stored = {
            document.id: document
            for document in session.scalars(
                select(Document).where(Document.bid_id == row.bid_id, Document.tenant_id == principal.tenant_id)
            )
        }
        previews = intake.get("preview_sources") or {}
        base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        documents = []
        for document in intake.get("documents") or []:
            item = dict(document)
            classification = item.get("classification") or {}
            item["document_type"] = classification.get("document_type") or "other"
            source_id = str(item.get("document_id") or "")
            preview = previews.get(source_id)
            if isinstance(preview, dict) and preview.get("object_key"):
                base = f"{base_url}/v1/workbench/projects/{project.id}/bids/{row.vendor_key}/sources/{source_id}"
                item["url"] = f"{base}/content"
                item["content_type"] = str(preview.get("content_type") or "application/octet-stream")
                if _preview_is_pdf(preview):
                    item["page_url_template"] = f"{base}/pages/{{page}}.png"
            else:
                source = stored.get(source_id)
                item["url"] = _document_item(source, "Bidder package")["url"] if source else None
            documents.append(item)
        return {
            "vendor_id": row.vendor_key,
            "bidder_name": row.vendor_name,
            "documents": documents,
            "quarantined_documents": [
                item for item in documents if "price" in item.get("document_type", "") or "commercial" in item.get("document_type", "")
            ],
            "source_excerpts": [],
            "tender_match": intake.get("tender_match")
            or {"status": "not_checked", "observed_tender_ids": []},
        }


def _bid_preview_source(
    session: Any,
    project_id: str,
    vendor_key: str,
    source_id: str,
    tenant_id: str,
) -> tuple[dict[str, Any], str]:
    project = _project(session, project_id, tenant_id)
    row = _bidder(session, project, vendor_key)
    source = (row.intake_json or {}).get("preview_sources", {}).get(source_id)
    if not isinstance(source, dict) or not source.get("object_key"):
        raise HTTPException(status_code=404, detail="bidder source preview not found")
    return source, str(source["object_key"])


@router.get("/projects/{project_id}/bids/{vendor_key}/sources/{source_id}/content")
def bid_source_content(
    project_id: str,
    vendor_key: str,
    source_id: str,
    request: Request,
) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        source, object_key = _bid_preview_source(
            session, project_id, vendor_key, source_id, principal.tenant_id
        )
        suffix, media_type, disposition = _preview_delivery(source)
        handle, name = tempfile.mkstemp(prefix="procurement-bid-source-", suffix=suffix)
        os.close(handle)
        path = Path(name)
        try:
            download_file(object_key, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            media_type=media_type,
            filename=Path(str(source.get("file_name") or f"bidder-source{suffix}")).name,
            content_disposition_type=disposition,
            headers={"X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(path.unlink, missing_ok=True),
        )


@router.get("/projects/{project_id}/bids/{vendor_key}/sources/{source_id}/pages/{page_number}.png")
def bid_source_page_image(
    project_id: str,
    vendor_key: str,
    source_id: str,
    page_number: int,
    request: Request,
) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    if page_number < 1 or page_number > int(os.getenv("MAX_PDF_PAGES", "500")):
        raise HTTPException(status_code=404, detail="document page not found")
    with session_scope(_sessions()) as session:
        source, object_key = _bid_preview_source(
            session, project_id, vendor_key, source_id, principal.tenant_id
        )
        if not _preview_is_pdf(source):
            raise HTTPException(status_code=415, detail="source does not have a PDF page preview")
        handle, source_name = tempfile.mkstemp(prefix="procurement-bid-page-", suffix=".pdf")
        os.close(handle)
        source_path = Path(source_name)
        image = source_path.with_suffix(".png")
        try:
            download_file(object_key, source_path)
            _render_pdf_page(source_path, image, page_number)
        except HTTPException:
            _unlink_many(source_path, image)
            raise
        except Exception as exc:
            _unlink_many(source_path, image)
            raise HTTPException(status_code=422, detail="document page could not be rendered") from exc
        return FileResponse(
            image,
            media_type="image/png",
            background=BackgroundTask(_unlink_many, source_path, image),
        )


@router.post("/projects/{project_id}/bids/{vendor_key}/classification/propose", status_code=status.HTTP_202_ACCEPTED)
def propose_classification(project_id: str, vendor_key: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _locked_project(session, project_id, principal.tenant_id)
        _require_frozen_checklist(session, project)
        row = _locked_bidder(session, project, vendor_key)
        if row.classification_status == "approved":
            raise HTTPException(status_code=409, detail="approved classification is immutable")
        active = _active_classification_operation(session, project.id, row.vendor_key)
        if active:
            return {"job_id": active.id, "status": active.status}
        operation = enqueue(
            session,
            tenant_id=principal.tenant_id,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="intake",
            payload={
                "project_id": project.id,
                "tender_version_id": project.tender_version_id,
                "vendor_key": row.vendor_key,
                "bid_id": row.bid_id,
            },
            key=_idempotency_key(request, required=True),
            request_hash=(
                f"{project.id}:{project.tender_version_id}:"
                f"{row.vendor_key}:{row.bid_id}:classification"
            ),
        )
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.classification.queued",
            entity_type="workbench_bid",
            entity_id=row.id,
            reason="classification proposal rebuilt",
            before={"status": row.classification_status},
            after={"operation_id": operation.id},
        )
        return {"job_id": operation.id, "status": operation.status}


@router.post("/projects/{project_id}/bids/{vendor_key}/classification/correct")
def correct_classification(
    project_id: str,
    vendor_key: str,
    body: ClassificationCorrection,
    request: Request,
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _locked_project(session, project_id, principal.tenant_id)
        _require_frozen_checklist(session, project)
        row = _locked_bidder(session, project, vendor_key)
        if row.classification_status == "approved":
            raise HTTPException(status_code=409, detail="approved classification is immutable")
        intake = dict(row.intake_json or {})
        classification = dict(intake.get("classification") or {})
        previous_classification = dict(classification)
        page_counts = {
            item.get("document_id"): int(item.get("page_count") or 1)
            for item in intake.get("documents") or []
        }
        if body.logical_documents is not None:
            for item in body.logical_documents:
                source_id = str(item.get("source_document_id") or "")
                start = int(item.get("start_page") or 0)
                end = int(item.get("end_page") or 0)
                if source_id not in page_counts or start < 1 or end < start or end > page_counts[source_id]:
                    raise HTTPException(status_code=422, detail="classification page range is invalid")
                if "broad_route" in item:
                    route = str(item.get("broad_route") or "")
                    if route not in _PHYSICAL_ROUTES and route not in _PHYSICAL_ROUTE_ALIASES:
                        raise HTTPException(status_code=422, detail="logical document route is invalid")
                    if _normalize_physical_route(route) == "mixed_submission":
                        raise HTTPException(status_code=422, detail="each logical document needs one classification route")
                if "secondary_routes" in item and any(
                    str(value) not in _PHYSICAL_ROUTES and str(value) not in _PHYSICAL_ROUTE_ALIASES
                    for value in item.get("secondary_routes") or []
                ):
                    raise HTTPException(status_code=422, detail="logical document secondary route is invalid")
            classification["reviewed_logical_documents"] = body.logical_documents
        if body.physical_routes is not None:
            normalized_routes = []
            for item in body.physical_routes:
                primary = _normalize_physical_route(item.get("broad_route"))
                secondary = []
                for value in item.get("secondary_routes") or []:
                    route = _normalize_physical_route(value)
                    if route not in {primary, "needs_review", "reference_document", "commercial_price_restricted"} and route not in secondary:
                        secondary.append(route)
                normalized_routes.append({
                    **item,
                    "broad_route": primary,
                    "secondary_routes": secondary,
                })
            if any(
                item.get("document_id") not in page_counts
                or item.get("broad_route") not in _PHYSICAL_ROUTES
                or (item.get("broad_route") == "needs_review" and not str(item.get("reason") or "").strip())
                for item in normalized_routes
            ):
                raise HTTPException(status_code=422, detail="physical document route is invalid")
            current_routes = {
                item.get("document_id"): item
                for item in _classification_payload(row)["classification"]["physical_routes"]
            }
            current_routes.update({item.get("document_id"): item for item in normalized_routes})
            classification["physical_routes"] = list(current_routes.values())
        intake["classification"] = classification
        sync_bidder_search_manifest(intake)
        row.intake_json = intake
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.classification.corrected",
            entity_type="workbench_bid",
            entity_id=row.id,
            reason=body.reason,
            before={"classification": previous_classification},
            after={"classification": classification, "reviewed_by": body.reviewed_by},
        )
        return _classification_payload(row)


@router.post("/projects/{project_id}/bids/{vendor_key}/classification/approve")
def approve_classification(
    project_id: str,
    vendor_key: str,
    body: ClassificationApproval,
    request: Request,
) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _locked_project(session, project_id, principal.tenant_id)
        row = _locked_bidder(session, project, vendor_key)
        if not row.intake_json:
            raise HTTPException(status_code=409, detail="bidder intake is not complete")
        if row.classification_status == "approved":
            raise HTTPException(status_code=409, detail="classification is already approved")
        tender_match = (row.intake_json or {}).get("tender_match") or {}
        if tender_match.get("status") == "mismatch":
            observed = ", ".join(tender_match.get("observed_tender_ids") or [])
            raise HTTPException(
                status_code=409,
                detail=f"bid package belongs to a different tender ({observed})",
            )
        payload = _classification_payload(row)
        unresolved = [
            item.get("logical_document_id")
            for item in payload["classification"]["logical_documents"]
            if item.get("needs_review") or item.get("broad_route") == "needs_review"
        ]
        if unresolved:
            raise HTTPException(
                status_code=409,
                detail=f"resolve every bidder document classification before approval ({len(unresolved)} remaining)",
            )
        intake = dict(row.intake_json or {})
        classification = dict(intake.get("classification") or {})
        classification["reviewed_logical_documents"] = payload["classification"]["logical_documents"]
        classification["physical_routes"] = payload["classification"]["physical_routes"]
        intake["classification"] = classification
        sync_bidder_search_manifest(intake)
        row.intake_json = intake
        row.classification_status = "approved"
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.classification.approved",
            entity_type="workbench_bid",
            entity_id=row.id,
            reason=body.reason,
            before={"status": "review_required"},
            after={"status": "approved", "reviewed_by": body.reviewed_by, "reviewed_at": body.reviewed_at},
        )
        return {"status": "approved", "state": _state(session, project)}


