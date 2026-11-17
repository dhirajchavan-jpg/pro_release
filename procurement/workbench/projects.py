"""Tender understanding, evidence, review, and project routes."""
from __future__ import annotations

import os
import tempfile
import hashlib
import json
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from starlette.background import BackgroundTask

from procurement.api import _actor_user_id, _hash, _owned, _sessions, _store_document, _upload_sha256
from procurement.domain.models import Document, Operation, Tender, TenderVersion, WorkbenchProject, new_id
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.object_store import download_file
from procurement.platform.operations import IdempotencyConflict, audit, enqueue, idempotent_result, remember_result
from procurement.workbench.compliance import compliance_gate_detail
from procurement.workbench.common import ContractDraftUpdate, FreezeContract, LedgerCorrection, ProjectUpdate, _current_project_operation, _document_item, _draft, _idempotency_key, _key, _ledger, _ledger_with_urls, _preview_delivery, _preview_is_pdf, _project, _render_pdf_page, _reviewed_contract_draft, _schema_from_text, _schema_projection, _source_urls, _state, _understanding, _unlink_many, _validate_field_schema

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])


def _base_url(request: Request) -> str:
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    project_key: str | None = Form(None),
    title: str | None = Form(None),
    field_schema_json: str | None = Form(None, alias="schema_json"),
    policy_audit: str | None = Form(None),
    policy_file: UploadFile | None = File(None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    idempotency_key = _idempotency_key(request, required=True)
    file_sha256 = _upload_sha256(file)
    key = _key(project_key or f"{file.filename or 'tender'}-{idempotency_key[-8:]}")
    schema = _schema_from_text(field_schema_json)
    request_hash = _hash({
        "action": "workbench.project.create",
        "project_key": key,
        "title": (title or "").strip(),
        "policy_audit": policy_audit,
        "field_schema": schema,
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
        if session.scalar(select(WorkbenchProject.id).where(WorkbenchProject.tenant_id == principal.tenant_id, WorkbenchProject.project_key == key)):
            raise HTTPException(status_code=409, detail="project key already exists")
        tender = Tender(id=new_id(), tenant_id=principal.tenant_id, external_reference=key, status="draft")
        session.add(tender)
        session.flush([tender])
        version = TenderVersion(
            id=new_id(),
            tender_id=tender.id,
            version_number=1,
            status="draft",
            understanding_json={
                "field_schema": schema,
                "policy_audit": {
                    "enabled": policy_audit is None
                    or str(policy_audit).strip().lower() in {"1", "true", "on", "yes"}
                },
            },
        )
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
        policy_document = None
        if policy_file is not None and policy_file.filename:
            policy_document = await _store_document(
                session, policy_file, tenant_id=principal.tenant_id, actor_id=actor_id,
                tender_id=tender.id, tender_version_id=version.id, bid_id=None,
                allowed_suffixes={".pdf", ".doc", ".docx", ".json", ".txt"},
                source_kind="compliance_policy",
            )
            version.understanding_json["policy_audit"] = {
                **dict(version.understanding_json.get("policy_audit") or {}),
                "enabled": True, "document_id": policy_document.id,
                "status": "needs_review", "file_name": policy_document.file_name,
                "sha256": policy_document.sha256,
            }
            flag_modified(version, "understanding_json")
        project = WorkbenchProject(id=new_id(), tenant_id=principal.tenant_id, tender_id=tender.id, tender_version_id=version.id, project_key=key, title=(title or "").strip() or None, stage="tender_uploaded")
        session.add(project)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="workbench.project.created", entity_type="workbench_project", entity_id=project.id, reason=None, before=None, after={"tender_id": tender.id, "document_id": document.id, "policy_document_id": policy_document.id if policy_document else None})
        session.flush()
        result = _state(session, project)
        result["field_schema"] = _schema_projection(schema)
        remember_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="project_create",
            result=result,
        )
        return result


@router.get("/field-schema/default")
def default_field_schema(request: Request) -> dict[str, Any]:
    require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    return _schema_projection(_schema_from_text(None))


@router.get("/projects/{project_id}/field-schema")
def project_field_schema(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        schema = data.get("field_schema")
        if not isinstance(schema, dict):
            schema = _schema_from_text(None)
        return _schema_projection(_validate_field_schema(schema))


@router.post("/projects/{project_id}/understand", status_code=status.HTTP_202_ACCEPTED)
def understand_project(
    project_id: str,
    request: Request,
    reviewed_by: str = Form(...),
    reviewed_at: str = Form(...),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        if project.stage != "tender_uploaded":
            raise HTTPException(status_code=409, detail="tender understanding has already started")
        version, _data = _understanding(session, project)
        existing = _current_project_operation(
            session,
            project,
            tenant_id=principal.tenant_id,
            stage="understanding",
            statuses=("queued", "running", "succeeded"),
            version=version,
        )
        if existing:
            return {"job_id": existing.id, "status": existing.status}
        operation = enqueue(
            session,
            tenant_id=principal.tenant_id,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="understanding",
            payload={
                "project_id": project.id,
                "tender_version_id": project.tender_version_id,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            },
            key=_idempotency_key(request, required=True),
            request_hash=_hash({
                "action": "workbench.understanding.run",
                "project_id": project.id,
                "tender_version_id": project.tender_version_id,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }),
        )
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.understanding.queued",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"stage": project.stage},
            after={"operation_id": operation.id},
        )
        return {"job_id": operation.id, "status": operation.status}


@router.get("/jobs/{operation_id}")
def workbench_job(operation_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        operation = _owned(session, Operation, operation_id, principal.tenant_id)
        public_status = "complete" if operation.status == "succeeded" else operation.status
        return {
            "job_id": operation.id,
            "status": public_status,
            "created_at": operation.created_at.isoformat() if operation.created_at else None,
            "started_at": operation.started_at.isoformat() if operation.started_at else None,
            "finished_at": operation.finished_at.isoformat() if operation.finished_at else None,
            "error_code": operation.error_code,
            "error": operation.error_detail,
            "result": operation.result_json,
        }


@router.get("/projects/{project_id}/ledger")
def get_ledger(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        _version, data = _understanding(session, project)
        if not data.get("tender_profile"):
            raise HTTPException(status_code=409, detail="tender understanding is not complete")
        documents = list(
            session.scalars(
                select(Document).where(
                    Document.tender_version_id == project.tender_version_id,
                    Document.tenant_id == principal.tenant_id,
                )
            )
        )
        return _ledger_with_urls(
            _ledger(data),
            _source_urls(data, documents, project.id, _base_url(request)),
        )


def _preview_source(session: Any, project_id: str, source_id: str, tenant_id: str) -> tuple[dict[str, Any], str]:
    project = _project(session, project_id, tenant_id)
    _version, data = _understanding(session, project)
    source = (data.get("preview_sources") or {}).get(source_id)
    if not isinstance(source, dict) or not source.get("object_key"):
        raise HTTPException(status_code=404, detail="tender source preview not found")
    return source, str(source["object_key"])


@router.get("/projects/{project_id}/sources/{source_id}/content")
def source_content(project_id: str, source_id: str, request: Request) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        source, object_key = _preview_source(session, project_id, source_id, principal.tenant_id)
        suffix, media_type, disposition = _preview_delivery(source)
        handle, name = tempfile.mkstemp(prefix="procurement-source-", suffix=suffix)
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
            filename=Path(str(source.get("file_name") or f"tender-source{suffix}")).name,
            content_disposition_type=disposition,
            headers={"X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(path.unlink, missing_ok=True),
        )


@router.get("/projects/{project_id}/sources/{source_id}/pages/{page_number}.png")
def source_page_image(project_id: str, source_id: str, page_number: int, request: Request) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    if page_number < 1 or page_number > int(os.getenv("MAX_PDF_PAGES", "500")):
        raise HTTPException(status_code=404, detail="document page not found")
    with session_scope(_sessions()) as session:
        source, object_key = _preview_source(session, project_id, source_id, principal.tenant_id)
        if not _preview_is_pdf(source):
            raise HTTPException(status_code=415, detail="source does not have a PDF page preview")
        handle, source_name = tempfile.mkstemp(prefix="procurement-source-page-", suffix=".pdf")
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


@router.get("/projects/{project_id}/documents")
def project_documents(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        _version, data = _understanding(session, project)
        documents = list(
            session.scalars(
                select(Document)
                .where(
                    Document.tender_version_id == project.tender_version_id,
                    Document.tenant_id == principal.tenant_id,
                )
                .order_by(Document.created_at)
            )
        )
        base_url = _base_url(request)
        items = [
            item
            for document in documents
            if (item := _document_item(document, "Tender package"))["url"]
        ]
        for item in items:
            item["url"] = base_url + item["url"]
        for source_id, source in (data.get("preview_sources") or {}).items():
            if not isinstance(source, dict) or not source.get("object_key"):
                continue
            item = {
                "document_id": source_id,
                "document_kind": "Tender source",
                "file_name": str(source.get("file_name") or source_id),
                "content_type": str(source.get("content_type") or "application/octet-stream"),
                "page_count": int(source.get("page_count") or 0),
                "url": f"{base_url}/v1/workbench/projects/{project.id}/sources/{source_id}/content",
            }
            if _preview_is_pdf(source):
                item["page_url_template"] = (
                    f"{base_url}/v1/workbench/projects/{project.id}/sources/{source_id}/pages/{{page}}.png"
                )
            items.append(item)
        return {"documents": items}


@router.get("/documents/{document_id}/content")
def document_content(document_id: str, request: Request) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        document = _owned(session, Document, document_id, principal.tenant_id)
        suffix = Path(document.file_name).suffix
        handle, name = tempfile.mkstemp(prefix="procurement-view-", suffix=suffix)
        os.close(handle)
        path = Path(name)
        try:
            download_file(document.object_key, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            media_type=document.content_type,
            filename=document.file_name,
            content_disposition_type="inline",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )


@router.get("/documents/{document_id}/pages/{page_number}.png")
def document_page_image(document_id: str, page_number: int, request: Request) -> FileResponse:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    if page_number < 1 or page_number > int(os.getenv("MAX_PDF_PAGES", "500")):
        raise HTTPException(status_code=404, detail="document page not found")
    with session_scope(_sessions()) as session:
        document = _owned(session, Document, document_id, principal.tenant_id)
        if Path(document.file_name).suffix.lower() != ".pdf":
            raise HTTPException(status_code=415, detail="page images are available only for PDF documents")
        handle, source_name = tempfile.mkstemp(prefix="procurement-page-source-", suffix=".pdf")
        os.close(handle)
        source = Path(source_name)
        image = source.with_suffix(".png")
        try:
            download_file(document.object_key, source)
            _render_pdf_page(source, image, page_number)
        except HTTPException:
            _unlink_many(source, image)
            raise
        except Exception as exc:
            _unlink_many(source, image)
            raise HTTPException(status_code=422, detail="document page could not be rendered") from exc
        return FileResponse(
            image,
            media_type="image/png",
            background=BackgroundTask(_unlink_many, source, image),
        )


@router.post("/projects/{project_id}/ledger/corrections")
def correct_ledger(project_id: str, body: LedgerCorrection, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        if version.status != "draft":
            raise HTTPException(status_code=409, detail="approved tender findings are immutable")
        corrections = list(data.get("ledger_corrections") or [])
        correction = body.model_dump()
        correction["recorded_at"] = datetime.now(timezone.utc).isoformat()
        corrections.append(correction)
        data["ledger_corrections"] = corrections
        ledger = _ledger(data)
        previous_entry: dict[str, Any] | None = None
        for entry in ledger["entries"]:
            if entry["key"] == body.field_key:
                previous_entry = copy.deepcopy(entry)
                ai_status = entry.get("status")
                ai_value = entry.get("value")
                entry.update(
                    status=body.status,
                    value=body.value,
                    conditions=body.conditions,
                    exceptions=body.exceptions,
                    required_documents=body.required_documents,
                    review_source="human_corrected",
                    verified=True,
                    blocking_grounding_issues=[],
                    ai_status=ai_status,
                    ai_value=ai_value,
                    correction_reason=body.reason,
                    reviewed_by=body.reviewed_by,
                    reviewed_at=body.reviewed_at,
                )
                break
        else:
            raise HTTPException(status_code=404, detail="ledger field not found")
        data["reviewed_ledger"] = ledger
        data.pop("contract_draft", None)
        version.understanding_json = data
        project.stage = "contract_drafted"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.ledger.corrected",
            entity_type="tender_version",
            entity_id=version.id,
            reason=body.reason,
            before={"field_key": body.field_key, "entry": previous_entry},
            after={
                "field_key": body.field_key,
                "entry": next(item for item in ledger["entries"] if item["key"] == body.field_key),
            },
        )
        draft = _draft(data)
        documents = list(
            session.scalars(
                select(Document).where(
                    Document.tender_version_id == project.tender_version_id,
                    Document.tenant_id == principal.tenant_id,
                )
            )
        )
        public_ledger = _ledger_with_urls(
            ledger,
            _source_urls(data, documents, project.id, _base_url(request)),
        )
        return {"ledger": public_ledger, "draft": draft, "state": _state(session, project)}


@router.get("/projects/{project_id}/contract/draft")
def get_contract_draft(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        _version, data = _understanding(session, project)
        if not data.get("requirement_matrix"):
            raise HTTPException(status_code=409, detail="tender understanding is not complete")
        return _draft(data)


@router.post("/projects/{project_id}/contract/draft")
def create_contract_draft(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        if not data.get("requirement_matrix"):
            raise HTTPException(status_code=409, detail="tender understanding is not complete")
        if version.status != "draft":
            raise HTTPException(status_code=409, detail="approved contract is immutable")
        gate = compliance_gate_detail(data)
        if gate:
            raise HTTPException(status_code=409, detail=gate)
        draft = _draft(data)
        draft["draft_id"] = str(draft.get("draft_id") or version.id)
        draft["tender_version_id"] = version.id
        draft["status"] = "draft"
        data["contract_draft"] = draft
        version.understanding_json = data
        project.stage = "contract_drafted"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.contract.drafted",
            entity_type="tender_version",
            entity_id=version.id,
            reason=None,
            before=None,
            after={"criteria": [item.get("criterion_id") for item in draft.get("criteria") or []]},
        )
        return {"draft": draft, "state": _state(session, project)}


@router.patch("/projects/{project_id}/contract/draft")
def update_contract_draft(
    project_id: str,
    body: ContractDraftUpdate,
    request: Request,
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version = session.scalar(
            select(TenderVersion)
            .where(TenderVersion.id == project.tender_version_id)
            .with_for_update()
        )
        if not version or version.tender_id != project.tender_id:
            raise HTTPException(status_code=404, detail="tender version not found")
        if version.status != "draft":
            raise HTTPException(status_code=409, detail="approved contract is immutable")
        data = dict(version.understanding_json or {})
        if not data.get("requirement_matrix"):
            raise HTTPException(status_code=409, detail="tender understanding is not complete")
        current = _draft(data)
        current["draft_id"] = str(current.get("draft_id") or version.id)
        current["tender_version_id"] = version.id
        current["status"] = "draft"
        current_revision = int(current.get("revision") or 0)
        submitted_revision = int(body.draft.get("revision") or body.revision)
        if body.revision != current_revision or submitted_revision != current_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONTRACT_REVISION_CONFLICT",
                    "message": "The checklist changed after it was loaded. Reload it before saving.",
                    "current_revision": current_revision,
                },
            )
        reviewed = _reviewed_contract_draft(
            current,
            body.draft,
            reason=body.reason,
            reviewed_by=body.reviewed_by,
            reviewed_at=body.reviewed_at,
        )
        reviewed["draft_id"] = current["draft_id"]
        reviewed["tender_version_id"] = version.id
        reviewed["status"] = "draft"
        data["contract_draft"] = reviewed
        version.understanding_json = data
        project.stage = "contract_drafted"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.contract.revised",
            entity_type="tender_version",
            entity_id=version.id,
            reason=body.reason,
            before={"revision": current_revision, "draft": current},
            after={"revision": reviewed["revision"], "draft": reviewed},
        )
        return {"draft": reviewed, "state": _state(session, project)}


@router.post("/projects/{project_id}/contract/freeze")
def freeze_contract(project_id: str, body: FreezeContract, request: Request) -> dict[str, Any]:
    from procurement.evaluation.contracts import CriterionContractError, normalize_contract

    principal = require_role(request, "admin", "approver")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        if version.status != "draft":
            raise HTTPException(status_code=409, detail="contract is already frozen")
        gate = compliance_gate_detail(data)
        if gate:
            raise HTTPException(status_code=409, detail=gate)
        canonical = _draft(data)
        if body.draft != canonical:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "The submitted contract draft does not match the current reviewed tender.",
                    "code": "CONTRACT_DRAFT_MISMATCH",
                },
            )
        coverage_gaps = canonical.get("unautomated_requirements") or []
        if coverage_gaps and not body.continue_with_limitations:
            raise HTTPException(status_code=409, detail={
                "message": "Source requirements remain unresolved; review them or explicitly continue with limitations.",
                "code": "CHECKLIST_SOURCE_COVERAGE_INCOMPLETE", "requirements": coverage_gaps,
            })
        active = [
            item
            for item in canonical.get("criteria") or []
            if (item.get("review") or {}).get("included") is not False
        ]
        excluded = [
            item
            for item in canonical.get("criteria") or []
            if (item.get("review") or {}).get("included") is False
        ]
        missing = {
            item.get("criterion_id", "criterion"): list(item.get("missing_fields") or [])
            for item in active
            if item.get("missing_fields") and not (item.get("review") or {}).get("confirmed")
        }
        if missing and not body.continue_with_limitations:
            raise HTTPException(status_code=409, detail={"message": "contract has unresolved fields", "missing_fields": missing})
        skipped = missing if body.continue_with_limitations else {}
        frozen = [copy.deepcopy(item) for item in active if item.get("criterion_id") not in skipped]
        for item in frozen:
            if (item.get("review") or {}).get("confirmed") and item.get("missing_fields"):
                item["review"]["confirmed_missing_fields"] = list(item["missing_fields"])
                item["missing_fields"] = []
                item["needs_human_input"] = False
        if not frozen:
            raise HTTPException(status_code=409, detail="contract has no evaluable criteria")
        try:
            # Validate the already-normalized reviewer draft, but persist the
            # exact approved representation so freeze is byte-for-byte stable.
            normalize_contract(frozen)
        except CriterionContractError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "The evaluation checklist contains contradictory or duplicate criteria.",
                    "code": "INVALID_CRITERION_CONTRACT",
                    "reason": str(exc),
                },
            ) from exc
        digest = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
        ).hexdigest()
        data["contract_draft"] = canonical
        data["frozen_contract"] = {
            "schema_version": "evaluation-checklist.v1",
            "checklist_id": str(canonical.get("draft_id") or version.id),
            "tender_version_id": version.id,
            "revision": int(canonical.get("revision") or 0),
            "content_sha256": digest,
            "criteria": frozen,
            "excluded_criteria": excluded,
            "skipped_criteria": skipped,
            "unautomated_requirements": copy.deepcopy(coverage_gaps),
            "reviewed_by": body.reviewed_by,
            "reviewed_at": body.reviewed_at,
        }
        version.understanding_json = data
        version.status = "approved"
        version.approved_by = actor_id
        version.approved_at = datetime.now(timezone.utc)
        project.stage = "contract_frozen"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.contract.frozen",
            entity_type="tender_version",
            entity_id=version.id,
            reason="continued with limitations" if skipped or coverage_gaps else None,
            before={"status": "draft"},
            after={
                "status": "approved",
                "revision": int(canonical.get("revision") or 0),
                "content_sha256": digest,
                "criteria": [item.get("criterion_id") for item in frozen],
                "excluded": [item.get("criterion_id") for item in excluded],
                "skipped": skipped,
            },
        )
        return {
            "state": _state(session, project),
            "frozen_criteria": [item.get("criterion_id") for item in frozen],
            "skipped_criteria": skipped,
        }


@router.get("/projects")
def list_projects(request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        projects = list(
            session.scalars(
                select(WorkbenchProject)
                .where(
                    WorkbenchProject.tenant_id == principal.tenant_id,
                    WorkbenchProject.stage != "deleted",
                )
                .order_by(WorkbenchProject.created_at.desc())
            )
        )
        return {"projects": [_state(session, project) for project in projects]}


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        return _state(session, _project(session, project_id, principal.tenant_id))


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        tender = session.get(Tender, project.tender_id)
        before = {"title": project.title, "tender_number": tender.external_reference if tender else None}
        if body.title is not None:
            project.title = body.title.strip() or None
        if tender and body.tender_number is not None:
            tender.external_reference = body.tender_number.strip() or None
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.project.updated",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before=before,
            after={"title": project.title, "tender_number": tender.external_reference if tender else None},
        )
        return _state(session, project)


