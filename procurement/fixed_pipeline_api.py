"""Fixed consumer contract over the existing procurement pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from procurement.api import _actor_user_id, _sessions
from procurement.domain.models import Document, Evaluation, Review, TenderVersion, WorkbenchBid, WorkbenchProject, new_id
from procurement.pipeline_api import EvaluationStart, _base_url, _classification_result, _tender_result, pipeline_job, start_bid_classification, start_bid_evaluation, start_tender_understanding
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from procurement.platform.operations import IdempotencyConflict, audit, idempotent_result, remember_result
from procurement.evaluation.validation import normalize_status_reason
from procurement.workbench.common import _PHYSICAL_ROUTE_ALIASES, _PHYSICAL_ROUTES, _classification_payload, _draft, _ledger, _project, sync_bidder_search_manifest

SCHEMA_VERSION = "procurement-fixed-tender.v1"
TENDER_REVIEW_VERSION = "procurement-fixed-tender-review.v1"
SCHEMA_ID = "tender-agent-reviewed-condensed-v2"
CLASSIFICATION_VERSION = "procurement-fixed-classification.v1"
CLASSIFICATION_REVIEW_VERSION = "procurement-fixed-classification-review.v1"
EVALUATION_VERSION = "procurement-fixed-evaluation.v1"
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "rfp_understanding.condensed-v2.json"
_CONFIG = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
_FIELD_DEFINITIONS = {
    field["key"]: field
    for section in _CONFIG["sections"]
    for field in section["fields"]
}
FIELD_KEYS = tuple(_FIELD_DEFINITIONS)
FIELD_STATUSES = {"found", "not_found", "not_applicable", "unresolved"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FullTenderReview(StrictModel):
    schema_version: Literal["procurement-fixed-tender-review.v1"]
    schema_id: Literal["tender-agent-reviewed-condensed-v2"]
    expected_revision: int = Field(ge=1)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=4000)
    fields: dict[str, dict[str, Any]]


class FullClassificationReview(StrictModel):
    schema_version: Literal["procurement-fixed-classification-review.v1"]
    expected_revision: int = Field(ge=1)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=4000)
    logical_documents: list[dict[str, Any]]
    physical_routes: list[dict[str, Any]]


class Approval(StrictModel):
    expected_revision: int = Field(ge=1)
    reviewed_by: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=3, max_length=4000)
    continue_with_limitations: bool = False


class FixedEvaluationStart(StrictModel):
    project_id: str = Field(min_length=1, max_length=36)
    vendor_id: str = Field(min_length=1, max_length=120)


router = APIRouter(prefix="/v1/pipeline/fixed-schema", tags=["fixed-schema pipeline"])


def _require_key(value: str | None) -> str:
    if not value or not value.strip():
        raise HTTPException(status_code=400, detail={"message": "Idempotency-Key header is required.", "code": "INVALID_REQUEST"})
    return value.strip()


def _revision(value: Any) -> int:
    return max(1, int(value or 1))


def _match_revision(expected: int, current: int, if_match: str | None) -> None:
    if if_match is None:
        raise HTTPException(status_code=400, detail={"message": "If-Match header is required.", "code": "INVALID_REQUEST"})
    try:
        header_revision = int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": "If-Match must contain the numeric revision.", "code": "INVALID_REQUEST"}) from exc
    if expected != header_revision or expected != current:
        raise HTTPException(
            status_code=409,
            detail={"message": "The resource changed after it was loaded.", "code": "REVISION_CONFLICT", "expected_revision": expected, "current_revision": current, "field_errors": []},
        )


def _request_digest(method: str, path: str, body: BaseModel) -> str:
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method}\0{path}\0{payload}".encode()).hexdigest()


def _idempotent_result(session: Any, tenant_id: str, key: str, digest: str) -> dict[str, Any] | None:
    try:
        return idempotent_result(session, tenant_id=tenant_id, key=key, request_hash=digest)
    except IdempotencyConflict as exc:
        code = "PRECONDITION_FAILED" if "incomplete" in str(exc) else "IDEMPOTENCY_CONFLICT"
        raise HTTPException(status_code=409, detail={"message": str(exc), "code": code}) from exc


def _remember_result(
    session: Any,
    *,
    tenant_id: str,
    key: str,
    digest: str,
    entity_type: str,
    entity_id: str,
    stage: str,
    result: dict[str, Any],
) -> None:
    remember_result(
        session,
        tenant_id=tenant_id,
        key=key,
        request_hash=digest,
        entity_type=entity_type,
        entity_id=entity_id,
        stage=stage,
        result=result,
    )


def _fixed_job(result: dict[str, Any], request: Request) -> dict[str, Any]:
    return {
        "api_version": "procurement-pipeline.v1",
        "job_id": result["job_id"],
        "status": result["status"],
        "job_url": f"{_base_url(request)}/v1/pipeline/fixed-schema/jobs/{result['job_id']}",
        "project_id": result["project_id"],
        "tender_id": result.get("tender_id"),
        "tender_version_id": result.get("tender_version_id"),
        "bid_id": result.get("bid_id"),
        "vendor_id": result.get("vendor_id"),
        "evaluation_id": result.get("evaluation_id"),
    }


def _field_value_valid(value_type: str, value: Any) -> bool:
    if value is None:
        return True
    if value_type in {"string", "date"}:
        return isinstance(value, str)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "money":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type.endswith("_array"):
        return isinstance(value, list)
    return isinstance(value, (str, int, float, bool, list, dict))


def _validate_fields(fields: dict[str, dict[str, Any]]) -> None:
    message = f"The complete {len(FIELD_KEYS)}-field review is invalid."
    missing = sorted(set(FIELD_KEYS) - set(fields))
    extra = sorted(set(fields) - set(FIELD_KEYS))
    if missing or extra:
        raise HTTPException(
            status_code=422,
            detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "missing_keys": missing, "unknown_keys": extra, "field_errors": []},
        )
    writable = {"status", "value", "conditions", "exceptions", "required_documents", "notes"}
    for key, item in fields.items():
        if not isinstance(item, dict) or set(item) != writable:
            raise HTTPException(status_code=422, detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "field_errors": [{"path": f"fields.{key}", "code": "INVALID_SHAPE", "message": f"Expected exactly {sorted(writable)}."}]})
        if item["status"] not in FIELD_STATUSES:
            raise HTTPException(status_code=422, detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "field_errors": [{"path": f"fields.{key}.status", "code": "INVALID_ENUM", "message": "Invalid field status."}]})
        for name in ("conditions", "exceptions", "required_documents"):
            if not isinstance(item[name], list) or any(not isinstance(value, str) for value in item[name]):
                raise HTTPException(status_code=422, detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "field_errors": [{"path": f"fields.{key}.{name}", "code": "INVALID_TYPE", "message": "Expected array."}]})
        if not isinstance(item["notes"], str):
            raise HTTPException(status_code=422, detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "field_errors": [{"path": f"fields.{key}.notes", "code": "INVALID_TYPE", "message": "Expected string."}]})
        if not _field_value_valid(_FIELD_DEFINITIONS[key].get("value_type", "string"), item["value"]):
            raise HTTPException(status_code=422, detail={"message": message, "code": "FIXED_SCHEMA_VALIDATION_FAILED", "field_errors": [{"path": f"fields.{key}.value", "code": "INVALID_TYPE", "message": f"Expected {_FIELD_DEFINITIONS[key].get('value_type')} or null."}]})


def _canonical_value(value_type: str, value: Any) -> Any:
    if value is None or _field_value_valid(value_type, value):
        return value
    if value_type == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if value_type in {"money", "number", "integer"} and isinstance(value, str):
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
        if match:
            number = float(match.group().replace(",", ""))
            return int(number) if value_type == "integer" or number.is_integer() else number
    if value_type == "boolean" and isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "applicable", "allowed", "required"}:
            return True
        if normalized in {"false", "no", "not applicable", "not allowed", "not required"}:
            return False
    if value_type == "string_array" and isinstance(value, str):
        return [value]
    return None


def _fixed_project(session: Any, project_id: str, tenant_id: str) -> WorkbenchProject:
    project = session.scalar(select(WorkbenchProject).where(WorkbenchProject.id == project_id, WorkbenchProject.tenant_id == tenant_id))
    if not project:
        raise HTTPException(status_code=404, detail={"message": "Project does not exist for this consumer.", "code": "PROJECT_NOT_FOUND"})
    return project


def _fixed_bidder(session: Any, project_id: str, vendor_id: str) -> WorkbenchBid:
    bidder = session.scalar(select(WorkbenchBid).where(WorkbenchBid.project_id == project_id, WorkbenchBid.vendor_key == vendor_id))
    if not bidder:
        raise HTTPException(status_code=404, detail={"message": "Bidder does not exist in this project.", "code": "BID_NOT_FOUND"})
    return bidder


def _fixed_tender(session: Any, project: WorkbenchProject, version: TenderVersion, request: Request) -> dict[str, Any]:
    source = _tender_result(session, project, version, request)
    ledger = source["ledger"]
    entries = {item["key"]: item for item in ledger["entries"]}
    fields = {}
    for key, definition in _FIELD_DEFINITIONS.items():
        item = entries.get(key) or {}
        value_type = definition.get("value_type") or "string"
        fields[key] = {
            "label": definition.get("label") or key,
            "value_type": value_type,
            "status": item.get("status") if item.get("status") in FIELD_STATUSES else "unresolved",
            "value": _canonical_value(value_type, item.get("value")),
            "conditions": [str(value) for value in item.get("conditions") or []],
            "exceptions": [str(value) for value in item.get("exceptions") or []],
            "required_documents": [str(value) for value in item.get("required_documents") or []],
            "notes": str(item.get("notes") or ""),
            "evidence": list(item.get("evidence") or []),
            "verified": bool(item.get("verified")),
            "review_source": "human_reviewed" if str(item.get("review_source") or "").startswith("human_") else "ai_extracted",
        }
    data = dict(version.understanding_json or {})
    base = _base_url(request)
    self_url = f"{base}/v1/pipeline/fixed-schema/tenders/{project.id}"
    documents = list(source["documents"])
    represented = {item["document_id"] for item in documents}
    for document in session.scalars(
        select(Document).where(
            Document.tender_version_id == version.id,
            Document.tenant_id == project.tenant_id,
        )
    ):
        if document.id in represented:
            continue
        content_url = f"{base}/v1/workbench/documents/{document.id}/content"
        documents.append({
            "document_id": document.id,
            "file_name": document.file_name,
            "content_type": document.content_type,
            "page_count": None,
            "content_url": content_url,
            "page_image_url_template": (
                f"{base}/v1/workbench/documents/{document.id}/pages/{{page}}.png"
                if Path(document.file_name).suffix.lower() == ".pdf"
                else None
            ),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_id": SCHEMA_ID,
        "project_id": project.id,
        "tender_id": project.tender_id,
        "tender_version_id": version.id,
        "revision": _revision(data.get("fixed_review_revision")),
        "review_status": "approved" if version.status == "approved" else "review_required",
        "editable": version.status != "approved",
        "fields": fields,
        "documents": documents,
        "links": {"self": self_url, "review": self_url, "approve": f"{self_url}/approve"},
    }


def _fixed_classification(session: Any, project: WorkbenchProject, bidder: WorkbenchBid, request: Request) -> dict[str, Any]:
    result = _classification_result(session, project, bidder, request)
    result.update(
        schema_version=CLASSIFICATION_VERSION,
        revision=_revision((bidder.intake_json or {}).get("fixed_review_revision")),
        editable=bidder.classification_status != "approved",
    )
    base = _base_url(request)
    self_url = f"{base}/v1/pipeline/fixed-schema/projects/{project.id}/bids/{bidder.vendor_key}/classification"
    result["links"] = {"self": self_url, "review": self_url, "approve": f"{self_url}/approve"}
    return result


def _references_with_urls(references: Any, documents: list[dict[str, Any]]) -> list[Any]:
    """Add authenticated document/page URLs without changing evidence identity."""
    by_id = {
        str(document.get("document_id") or ""): document
        for document in documents
        if str(document.get("document_id") or "")
    }
    by_name = {
        str(document.get("file_name") or "").casefold(): document
        for document in documents
        if str(document.get("file_name") or "")
    }
    hydrated: list[Any] = []
    for raw in references if isinstance(references, list) else []:
        if not isinstance(raw, dict):
            hydrated.append(raw)
            continue
        item = dict(raw)
        identifier = next(
            (
                str(item.get(key))
                for key in ("document_id", "documentId", "source_id", "sourceId")
                if item.get(key) not in (None, "")
            ),
            "",
        )
        name = str(item.get("file_name") or item.get("filename") or "")
        document = by_id.get(identifier) or by_name.get(name.casefold())
        if document:
            item.setdefault("document_id", document.get("document_id"))
            item.setdefault("file_name", document.get("file_name"))
            item["pdf_url"] = document.get("content_url")
            page = item.get("page") or item.get("page_number") or item.get("pageNumber")
            template = document.get("page_image_url_template")
            item["page_image_url"] = template.format(page=page) if template and page else None
        hydrated.append(item)
    return hydrated


@router.post("/tenders", status_code=status.HTTP_202_ACCEPTED)
async def create_tender(
    request: Request,
    file: UploadFile = File(...),
    project_key: str | None = Form(None),
    title: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_key(idempotency_key)
    return _fixed_job(await start_tender_understanding(request, file, None, project_key, title), request)


@router.get("/tenders/{project_id}")
def get_tender(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _fixed_project(session, project_id, principal.tenant_id)
        version = session.get(TenderVersion, project.tender_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="tender version not found")
        return _fixed_tender(session, project, version, request)


@router.put("/tenders/{project_id}")
def replace_tender(
    project_id: str,
    body: FullTenderReview,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    key = _require_key(idempotency_key)
    _validate_fields(body.fields)
    digest = _request_digest("PUT", request.url.path, body)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _fixed_project(session, project_id, principal.tenant_id)
        version = session.scalar(select(TenderVersion).where(TenderVersion.id == project.tender_version_id).with_for_update())
        if not version:
            raise HTTPException(status_code=404, detail="tender version not found")
        existing = _idempotent_result(session, principal.tenant_id, key, digest)
        if existing:
            return existing
        if version.status == "approved":
            raise HTTPException(status_code=409, detail={"message": "Approved tender cannot be overwritten.", "code": "RESOURCE_FROZEN"})
        data = dict(version.understanding_json or {})
        current = _revision(data.get("fixed_review_revision"))
        _match_revision(body.expected_revision, current, if_match)
        old_entries = {item["key"]: item for item in _ledger(data)["entries"]}
        entries = []
        for field_key in FIELD_KEYS:
            submitted = body.fields[field_key]
            previous = old_entries.get(field_key) or {}
            entries.append({
                **previous,
                "key": field_key,
                "label": _FIELD_DEFINITIONS[field_key].get("label") or field_key,
                **submitted,
                "verified": True,
                "review_source": "human_corrected",
                "blocking_grounding_issues": [],
            })
        data["reviewed_ledger"] = {
            **_ledger(data),
            "editable": True,
            "ledger_status": "needs_review" if any(item["status"] == "unresolved" for item in entries) else "complete",
            "entries": entries,
        }
        data.pop("contract_draft", None)
        data["fixed_review_revision"] = current + 1
        version.understanding_json = data
        result = _fixed_tender(session, project, version, request)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="fixed_schema.tender.replaced", entity_type="tender_version", entity_id=version.id, reason=body.reason, before={"revision": current}, after={"revision": current + 1, "reviewed_by": body.reviewed_by})
        _remember_result(session, tenant_id=principal.tenant_id, key=key, digest=digest, entity_type="tender_version", entity_id=version.id, stage="fixed_tender_review", result=result)
        return result


@router.post("/tenders/{project_id}/approve")
def approve_tender(
    project_id: str,
    body: Approval,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver", "reviewer")
    key = _require_key(idempotency_key)
    digest = _request_digest("POST", request.url.path, body)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _fixed_project(session, project_id, principal.tenant_id)
        version = session.scalar(select(TenderVersion).where(TenderVersion.id == project.tender_version_id).with_for_update())
        if not version:
            raise HTTPException(status_code=404, detail="tender version not found")
        existing = _idempotent_result(session, principal.tenant_id, key, digest)
        if existing:
            return existing
        data = dict(version.understanding_json or {})
        current = _revision(data.get("fixed_review_revision"))
        if body.expected_revision != current:
            raise HTTPException(status_code=409, detail={"message": "The resource changed after it was loaded.", "code": "REVISION_CONFLICT", "expected_revision": body.expected_revision, "current_revision": current, "field_errors": []})
        if version.status == "approved":
            raise HTTPException(status_code=409, detail={"message": "Approved tender cannot be overwritten.", "code": "RESOURCE_FROZEN"})
        draft = _draft(data)
        active = [
            item
            for item in draft.get("criteria") or []
            if (item.get("review") or {}).get("included") is not False
        ]
        excluded = [
            item
            for item in draft.get("criteria") or []
            if (item.get("review") or {}).get("included") is False
        ]
        unresolved = [item for item in active if item.get("needs_human_input")]
        if unresolved and not body.continue_with_limitations:
            raise HTTPException(status_code=409, detail={"message": "Previous required workflow stage is incomplete.", "code": "PRECONDITION_FAILED", "unresolved_criteria": len(unresolved)})
        skipped = {
            str(item.get("criterion_id") or "criterion"): list(item.get("missing_fields") or [])
            for item in unresolved
        } if body.continue_with_limitations else {}
        frozen = [
            item
            for item in active
            if str(item.get("criterion_id") or "") not in skipped
        ]
        if not frozen:
            raise HTTPException(
                status_code=409,
                detail={"message": "No evaluable tender criteria remain after review.", "code": "PRECONDITION_FAILED"},
            )
        approved_at = datetime.now(timezone.utc)
        data["contract_draft"] = draft
        content_sha256 = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
        ).hexdigest()
        data["frozen_contract"] = {
            **draft,
            "criteria": frozen,
            "excluded_criteria": excluded,
            "skipped_criteria": skipped,
            "checklist_id": str(draft.get("draft_id") or version.id),
            "tender_version_id": version.id,
            "content_sha256": content_sha256,
            "reviewed_by": body.reviewed_by,
            "reviewed_at": approved_at.isoformat(),
        }
        data["fixed_review_revision"] = current + 1
        version.understanding_json = data
        version.status = "approved"
        version.approved_by = actor_id
        version.approved_at = approved_at
        project.stage = "contract_frozen"
        project.updated_at = approved_at
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="fixed_schema.tender.approved", entity_type="tender_version", entity_id=version.id, reason=body.reason, before={"status": "draft"}, after={"status": "approved", "revision": current + 1, "reviewed_by": body.reviewed_by, "criteria": [item.get("criterion_id") for item in frozen], "skipped": skipped})
        result = {"schema_version": SCHEMA_VERSION, "project_id": project.id, "tender_version_id": version.id, "revision": current + 1, "review_status": "approved", "editable": False}
        _remember_result(session, tenant_id=principal.tenant_id, key=key, digest=digest, entity_type="tender_version", entity_id=version.id, stage="fixed_tender_approval", result=result)
        return result


@router.post("/bids", status_code=status.HTTP_202_ACCEPTED)
async def create_classification(
    request: Request,
    file: UploadFile = File(...),
    project_id: str = Form(...),
    vendor_name: str = Form(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_key(idempotency_key)
    return _fixed_job(await start_bid_classification(request, file, project_id, vendor_name), request)


@router.get("/projects/{project_id}/bids/{vendor_id}/classification")
def get_classification(project_id: str, vendor_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _fixed_project(session, project_id, principal.tenant_id)
        bidder = _fixed_bidder(session, project.id, vendor_id)
        return _fixed_classification(session, project, bidder, request)


def _validate_classification(body: FullClassificationReview, bidder: WorkbenchBid) -> None:
    intake = dict(bidder.intake_json or {})
    page_counts = {str(item.get("document_id")): int(item.get("page_count") or 1) for item in intake.get("documents") or []}
    logical_ids: set[str] = set()
    for item in body.logical_documents:
        required = {"logical_document_id", "title", "document_type", "source_document_id", "start_page", "end_page", "content_summary", "key_facts", "confidence"}
        optional = {"broad_route", "secondary_routes", "evidence_purposes", "needs_review", "route_reason"}
        if not required.issubset(item) or set(item) - required - optional or str(item.get("logical_document_id") or "") in logical_ids:
            raise HTTPException(status_code=422, detail={"message": "Classification review is invalid.", "code": "CLASSIFICATION_VALIDATION_FAILED"})
        logical_ids.add(str(item["logical_document_id"]))
        source_id = str(item["source_document_id"])
        start, end = int(item["start_page"]), int(item["end_page"])
        if source_id not in page_counts or start < 1 or end < start or end > page_counts[source_id]:
            raise HTTPException(status_code=422, detail={"message": "Classification review is invalid.", "code": "CLASSIFICATION_VALIDATION_FAILED"})
        if "broad_route" in item:
            route = str(item.get("broad_route") or "")
            if route not in _PHYSICAL_ROUTES | _PHYSICAL_ROUTE_ALIASES.keys() or route == "mixed_submission":
                raise HTTPException(status_code=422, detail={"message": "Each logical document needs one classification route.", "code": "CLASSIFICATION_VALIDATION_FAILED"})
        if "secondary_routes" in item and (
            not isinstance(item.get("secondary_routes"), list)
            or any(route not in _PHYSICAL_ROUTES | _PHYSICAL_ROUTE_ALIASES.keys() for route in item.get("secondary_routes") or [])
        ):
            raise HTTPException(status_code=422, detail={"message": "Classification review is invalid.", "code": "CLASSIFICATION_VALIDATION_FAILED"})
    route_ids = [str(item.get("document_id") or "") for item in body.physical_routes]
    if len(route_ids) != len(set(route_ids)) or set(route_ids) != set(page_counts):
        raise HTTPException(status_code=422, detail={"message": "Classification review is invalid.", "code": "CLASSIFICATION_VALIDATION_FAILED"})
    for item in body.physical_routes:
        required = {"document_id", "broad_route", "secondary_routes", "evidence_purposes", "reason"}
        secondary = item.get("secondary_routes")
        purposes = item.get("evidence_purposes")
        if (
            set(item) != required
            or item["broad_route"] not in _PHYSICAL_ROUTES | _PHYSICAL_ROUTE_ALIASES.keys()
            or not isinstance(secondary, list)
            or any(route not in _PHYSICAL_ROUTES | _PHYSICAL_ROUTE_ALIASES.keys() for route in secondary)
            or len(secondary) != len(set(secondary))
            or not isinstance(purposes, list)
            or any(not isinstance(value, str) for value in purposes)
            or not isinstance(item.get("reason"), str)
        ):
            raise HTTPException(status_code=422, detail={"message": "Classification review is invalid.", "code": "CLASSIFICATION_VALIDATION_FAILED"})


@router.put("/projects/{project_id}/bids/{vendor_id}/classification")
def replace_classification(
    project_id: str,
    vendor_id: str,
    body: FullClassificationReview,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    key = _require_key(idempotency_key)
    digest = _request_digest("PUT", request.url.path, body)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _fixed_project(session, project_id, principal.tenant_id)
        bidder = session.scalar(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_id).with_for_update())
        if not bidder:
            raise HTTPException(status_code=404, detail={"message": "Bidder does not exist in this project.", "code": "BID_NOT_FOUND"})
        existing = _idempotent_result(session, principal.tenant_id, key, digest)
        if existing:
            return existing
        if bidder.classification_status == "approved":
            raise HTTPException(status_code=409, detail={"message": "Approved classification cannot be overwritten.", "code": "RESOURCE_FROZEN"})
        _validate_classification(body, bidder)
        intake = dict(bidder.intake_json or {})
        current = _revision(intake.get("fixed_review_revision"))
        _match_revision(body.expected_revision, current, if_match)
        classification = dict(intake.get("classification") or {})
        classification["reviewed_logical_documents"] = body.logical_documents
        classification["physical_routes"] = body.physical_routes
        intake["classification"] = classification
        sync_bidder_search_manifest(intake)
        intake["fixed_review_revision"] = current + 1
        bidder.intake_json = intake
        result = _fixed_classification(session, project, bidder, request)
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="fixed_schema.classification.replaced", entity_type="workbench_bid", entity_id=bidder.id, reason=body.reason, before={"revision": current}, after={"revision": current + 1, "reviewed_by": body.reviewed_by})
        _remember_result(session, tenant_id=principal.tenant_id, key=key, digest=digest, entity_type="workbench_bid", entity_id=bidder.id, stage="fixed_classification_review", result=result)
        return result


@router.post("/projects/{project_id}/bids/{vendor_id}/classification/approve")
def approve_classification(
    project_id: str,
    vendor_id: str,
    body: Approval,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "approver", "reviewer")
    key = _require_key(idempotency_key)
    digest = _request_digest("POST", request.url.path, body)
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _fixed_project(session, project_id, principal.tenant_id)
        bidder = session.scalar(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id, WorkbenchBid.vendor_key == vendor_id).with_for_update())
        if not bidder:
            raise HTTPException(status_code=404, detail={"message": "Bidder does not exist in this project.", "code": "BID_NOT_FOUND"})
        existing = _idempotent_result(session, principal.tenant_id, key, digest)
        if existing:
            return existing
        intake = dict(bidder.intake_json or {})
        current = _revision(intake.get("fixed_review_revision"))
        if body.expected_revision != current:
            raise HTTPException(status_code=409, detail={"message": "The resource changed after it was loaded.", "code": "REVISION_CONFLICT", "expected_revision": body.expected_revision, "current_revision": current, "field_errors": []})
        if bidder.classification_status == "approved":
            raise HTTPException(status_code=409, detail={"message": "Approved classification cannot be overwritten.", "code": "RESOURCE_FROZEN"})
        tender_match = intake.get("tender_match") or {}
        if tender_match.get("status") == "mismatch":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Bid package belongs to a different tender.",
                    "code": "TENDER_MISMATCH",
                    "observed_tender_ids": tender_match.get("observed_tender_ids") or [],
                },
            )
        payload = _classification_payload(bidder)
        unresolved = [item["logical_document_id"] for item in payload["classification"]["logical_documents"] if item.get("needs_review") or item.get("broad_route") == "needs_review"]
        if unresolved:
            raise HTTPException(status_code=409, detail={"message": "Previous required workflow stage is incomplete.", "code": "PRECONDITION_FAILED", "document_ids": unresolved})
        classification = dict(intake.get("classification") or {})
        classification["reviewed_logical_documents"] = payload["classification"]["logical_documents"]
        classification["physical_routes"] = payload["classification"]["physical_routes"]
        intake["classification"] = classification
        sync_bidder_search_manifest(intake)
        intake["fixed_review_revision"] = current + 1
        bidder.intake_json = intake
        bidder.classification_status = "approved"
        audit(session, tenant_id=principal.tenant_id, actor_user_id=actor_id, event_type="fixed_schema.classification.approved", entity_type="workbench_bid", entity_id=bidder.id, reason=body.reason, before={"status": "review_required"}, after={"status": "approved", "revision": current + 1, "reviewed_by": body.reviewed_by})
        result = {"schema_version": CLASSIFICATION_VERSION, "project_id": project.id, "bid_id": bidder.bid_id, "vendor_id": bidder.vendor_key, "revision": current + 1, "review_status": "approved", "editable": False}
        _remember_result(session, tenant_id=principal.tenant_id, key=key, digest=digest, entity_type="workbench_bid", entity_id=bidder.id, stage="fixed_classification_approval", result=result)
        return result


@router.post("/bids/evaluate", status_code=status.HTTP_202_ACCEPTED)
def create_evaluation(
    body: FixedEvaluationStart,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_key(idempotency_key)
    return _fixed_job(start_bid_evaluation(EvaluationStart(**body.model_dump()), request), request)


@router.get("/projects/{project_id}/bids/{vendor_id}/evaluation")
def get_evaluation(project_id: str, vendor_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _fixed_project(session, project_id, principal.tenant_id)
        bidder = _fixed_bidder(session, project.id, vendor_id)
        evaluation = session.get(Evaluation, bidder.evaluation_id) if bidder.evaluation_id else None
        if not evaluation:
            raise HTTPException(status_code=404, detail={"message": "Evaluation has not been created.", "code": "EVALUATION_NOT_FOUND"})
        public = dict(evaluation.result_json or {})
        internal_report = ((evaluation.internal_result_json or {}).get("report") or {})
        internal_findings = list(internal_report.get("findings") or [])
        findings_by_id = {
            str(item.get("criterion_id") or ""): item
            for item in internal_findings
            if str(item.get("criterion_id") or "")
        }
        internal_criteria = list(((internal_report.get("tender") or {}).get("criteria") or []))
        latest_reviews: dict[str, Review] = {}
        for review in session.scalars(
            select(Review).where(Review.evaluation_id == evaluation.id).order_by(Review.created_at)
        ):
            payload = review.payload_json or {}
            identifier = str(payload.get("criterion_id") or payload.get("checkpoint_id") or "")
            if identifier:
                latest_reviews[identifier.removeprefix("BHEL-").casefold()] = review
        classification = _classification_result(session, project, bidder, request)
        documents = {item["document_id"]: item for item in classification["documents"]}
        criteria = []
        for index, item in enumerate(public.get("criteria_results") or []):
            number = item.get("no") if isinstance(item.get("no"), int) else index + 1
            normalized_requirement = " ".join(str(item.get("requirement") or "").casefold().split())
            semantic_matches = [
                candidate
                for candidate in internal_criteria
                if " ".join(str(candidate.get("requirement") or "").casefold().split())
                == normalized_requirement
            ]
            # New evaluations join by semantic identity before using the old
            # ordinal fallback.  The fallback remains only for stored legacy
            # reports whose public rows did not carry criterion identifiers.
            internal_criterion = (
                semantic_matches[0]
                if len(semantic_matches) == 1
                else internal_criteria[number - 1]
                if 0 < number <= len(internal_criteria)
                else {}
            )
            criterion_id = str(internal_criterion.get("criterion_id") or "")
            finding = findings_by_id.get(criterion_id) or {}
            if finding:
                finding = {
                    **finding,
                    "reason": normalize_status_reason(finding.get("status"), finding.get("reason")),
                }
            if not finding:
                matches = [
                    candidate
                    for candidate in internal_findings
                    if " ".join(str(candidate.get("criterion_text") or "").casefold().split())
                    == " ".join(str(item.get("requirement") or "").casefold().split())
                ]
                if len(matches) == 1:
                    finding = matches[0]
                    criterion_id = str(finding.get("criterion_id") or "")
            review = latest_reviews.get(criterion_id.removeprefix("BHEL-").casefold())
            review_payload = dict(review.payload_json or {}) if review else {}
            decision = str(review_payload.get("decision") or review.action or "") if review else ""
            effective_status = item.get("status") or finding.get("status") or "review"
            effective_result = item.get("result") or ""
            effective_why = normalize_status_reason(
                effective_status,
                item.get("why") or finding.get("reason") or "",
            )
            if decision == "meets_requirement":
                effective_status, effective_result = "passed", "Passed"
            elif decision == "does_not_meet":
                effective_status, effective_result = "failed", "Failed"
            elif decision:
                effective_status, effective_result = "needs_manual_review", "Needs manual review"
            if review:
                effective_why = str(review.reason or review_payload.get("reason") or effective_why)
            evidence = []
            for source in finding.get("evidence") or []:
                document = documents.get(str(source.get("document_id") or "")) or {}
                page = source.get("page") or source.get("page_number")
                template = document.get("page_image_url_template")
                evidence.append({
                    "document_id": str(source.get("document_id") or ""),
                    "file_name": str(source.get("file_name") or document.get("file_name") or ""),
                    "page": page,
                    "quote": str(source.get("quote") or ""),
                    "pdf_url": document.get("content_url"),
                    "page_image_url": template.format(page=page) if template and page else None,
                })
            criteria.append({
                "criterion_id": criterion_id or f"criterion-{number}",
                "no": number,
                "category": item.get("category") or finding.get("category") or "other",
                "requirement": item.get("requirement") or finding.get("criterion_text") or "",
                "mandatory": bool(finding.get("mandatory", True)),
                "status": effective_status,
                "result": effective_result,
                "why": effective_why,
                "required_documents": item.get("required_documents") or finding.get("required_documents") or [],
                "missing_documents": item.get("missing_documents") or finding.get("missing_documents") or [],
                "evidence": evidence,
            })
        passed = {"pass", "passed", "meets_requirement"}
        failed = {"fail", "failed", "does_not_meet"}
        review_ids = [item["criterion_id"] for item in criteria if item["status"] not in passed | failed | {"missing"}]
        has_mandatory_blocker = any(
            item["mandatory"] and item["status"] in failed | {"missing"}
            for item in criteria
        )
        qualifies = False if has_mandatory_blocker else None if review_ids else public.get("qualifies")
        outcome = (
            "does_not_qualify"
            if has_mandatory_blocker
            else "review_required"
            if review_ids or qualifies is None
            else "qualifies"
            if qualifies is True
            else "does_not_qualify"
        )
        recommendation = "rejected" if has_mandatory_blocker else "needs_manual_review" if review_ids else public.get("recommendation") or "technically_qualified"
        tender = public.get("tender") or {}
        vendor = public.get("vendor") or {}
        summary = {
            "outcome": outcome,
            "qualifies": qualifies,
            "recommendation": recommendation,
            "passed": sum(item.get("status") in passed for item in criteria),
            "failed": sum(item.get("status") in failed for item in criteria),
            "missing": sum(item.get("status") == "missing" for item in criteria),
            "needs_manual_review": sum(item.get("status") not in passed | failed | {"missing"} for item in criteria),
            "total": len(criteria),
            "text": public.get("summary") or "",
            "next_action": public.get("next_action") or "",
        }
        internal_reasons = list((((evaluation.internal_result_json or {}).get("report") or {}).get("manual_review_reasons") or []))
        public_manual_review = public.get("manual_review") if isinstance(public.get("manual_review"), dict) else {}
        review_reasons = list(public_manual_review.get("reasons") or internal_reasons)
        if review_ids and not review_reasons:
            review_reasons = [f"{criterion_id} requires manual review." for criterion_id in review_ids]
        return {
            "schema_version": EVALUATION_VERSION,
            "project_id": project.id,
            "bid_id": bidder.bid_id,
            "vendor_id": bidder.vendor_key,
            "evaluation_id": evaluation.id,
            "status": "complete" if evaluation.status == "succeeded" else evaluation.status,
            "summary": summary,
            "tender": {
                "title": tender.get("title") or "",
                "tender_number": tender.get("tender_number") or "",
                "buyer": tender.get("buyer") or "",
                "procurement_type": tender.get("procurement_type") or "",
                "bid_system": tender.get("bid_system") or "",
                "scope": tender.get("scope") or "",
                "summary": tender.get("summary") or "",
                "mandatory_documents": list(tender.get("mandatory_documents") or []),
            },
            "vendor": {
                "name": vendor.get("name") or bidder.vendor_name,
                "identifiers": dict(vendor.get("identifiers") or {}),
                "submitted_documents": list(vendor.get("submitted_documents") or []),
                "missing_documents": list(vendor.get("missing_documents") or []),
            },
            "criteria_results": criteria,
            "missing_documents": public.get("missing_documents") or [],
            "manual_review": {"required": bool(review_ids or public_manual_review.get("required")), "reasons": review_reasons},
            "financial_opening": public.get("financial_opening") or {},
            "validation": public.get("validation") or {},
            "links": {
                "self": f"{_base_url(request)}/v1/pipeline/fixed-schema/projects/{project.id}/bids/{bidder.vendor_key}/evaluation",
                "report": f"{_base_url(request)}/v1/workbench/projects/{project.id}/report",
            },
        }


@router.get("/projects/{project_id}/bids/{vendor_id}/evaluation/bhel")
def get_bhel_evaluation(project_id: str, vendor_id: str, request: Request) -> dict[str, Any]:
    """Return the versioned BHEL view without changing the fixed v1 contract."""
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _fixed_project(session, project_id, principal.tenant_id)
        bidder = _fixed_bidder(session, project.id, vendor_id)
        evaluation = session.get(Evaluation, bidder.evaluation_id) if bidder.evaluation_id else None
        if not evaluation or evaluation.status != "succeeded":
            raise HTTPException(
                status_code=409,
                detail={"message": "BHEL evaluation is not ready.", "code": "BHEL_EVALUATION_NOT_READY"},
            )
        internal = dict(evaluation.internal_result_json or {})
        report = dict(internal.get("report") or {})
        extension = dict(((report.get("extensions") or {}).get("bhel") or {}))
        if not extension:
            from procurement.bhel_evaluation import build_bhel_extension

            handoffs = dict(internal.get("handoffs") or {})
            extension = build_bhel_extension(
                handoffs.get("04_requirement_matrix") or {},
                report.get("findings") or handoffs.get("08_criteria_findings") or [],
                report,
            )
        if not extension:
            raise HTTPException(
                status_code=409,
                detail={"message": "The BHEL evaluation profile is not enabled.", "code": "BHEL_PROFILE_NOT_ENABLED"},
            )
        from procurement.bhel_evaluation import apply_bhel_human_reviews

        reviews = list(
            session.scalars(
                select(Review)
                .where(Review.evaluation_id == evaluation.id)
                .order_by(Review.created_at, Review.id)
            )
        )
        extension = apply_bhel_human_reviews(extension, reviews)
        version = session.get(TenderVersion, project.tender_version_id)
        tender_documents = _tender_result(session, project, version, request)["documents"] if version else []
        bidder_documents = _classification_result(session, project, bidder, request)["documents"]
        for section in ("checks", "internal_controls"):
            for check in extension.get(section) or []:
                check["source_references"] = _references_with_urls(
                    check.get("source_references"),
                    tender_documents,
                )
                check["bidder_evidence"] = _references_with_urls(
                    check.get("bidder_evidence"),
                    bidder_documents,
                )
        return {
            **extension,
            "project_id": project.id,
            "bid_id": bidder.bid_id,
            "vendor_id": bidder.vendor_key,
            "evaluation_id": evaluation.id,
        }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        result = pipeline_job(job_id, request)
    except HTTPException as exc:
        if exc.status_code == 404:
            exc.detail = {"message": "Job does not exist for this consumer.", "code": "JOB_NOT_FOUND"}
        raise
    return {
        "api_version": "procurement-pipeline.v1",
        "job_id": result["job_id"],
        "stage": "classification" if result["stage"] == "intake" else result["stage"],
        "status": result["status"],
        "result_type": result.get("result_type"),
        "error_code": result.get("error_code"),
        "error": result.get("error"),
    }
