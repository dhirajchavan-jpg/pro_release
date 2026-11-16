"""Project state, schema validation, and document delivery helpers."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select

from procurement.domain.models import (
    Bid,
    Document,
    Evaluation,
    Operation,
    TenderVersion,
    WorkbenchBid,
    WorkbenchProject,
)


_DEFAULT_FIELD_SCHEMA = Path(__file__).resolve().parents[2] / "config" / "rfp_understanding.condensed-v2.json"
_PLAIN_TEXT_PREVIEW_SUFFIXES = {".csv", ".htm", ".html", ".json", ".md", ".txt", ".xml"}


def _preview_is_pdf(source: dict[str, Any]) -> bool:
    return str(source.get("content_type") or "").partition(";")[0].strip().lower() == "application/pdf"


def _preview_delivery(source: dict[str, Any]) -> tuple[str, str, str]:
    file_name = Path(str(source.get("file_name") or "source.bin")).name
    suffix = Path(file_name).suffix.lower() or ".bin"
    if _preview_is_pdf(source):
        return ".pdf", "application/pdf", "inline"
    if suffix in _PLAIN_TEXT_PREVIEW_SUFFIXES:
        return suffix, "text/plain; charset=utf-8", "inline"
    return suffix, str(source.get("content_type") or "application/octet-stream"), "attachment"


def _validate_field_schema(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != "rfp-field-schema.v1":
        raise HTTPException(status_code=400, detail="field schema is not a valid rfp-field-schema.v1 document")
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise HTTPException(status_code=400, detail="field schema must contain sections")
    keys: set[str] = set()
    for section in sections:
        fields = section.get("fields") if isinstance(section, dict) else None
        if not str(section.get("key") or "").strip() or not isinstance(fields, list) or not fields:
            raise HTTPException(status_code=400, detail="every field schema section must have a key and fields")
        for field in fields:
            key = str(field.get("key") or "").strip() if isinstance(field, dict) else ""
            if not key or key in keys:
                raise HTTPException(status_code=400, detail="field schema field keys must be non-empty and unique")
            keys.add(key)
    return payload


def _schema_from_text(value: str | None) -> dict[str, Any]:
    if value is None:
        try:
            return _validate_field_schema(json.loads(_DEFAULT_FIELD_SCHEMA.read_text(encoding="utf-8")))
        except OSError as exc:
            raise HTTPException(status_code=503, detail="default field schema is unavailable") from exc
    if len(value.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="field schema exceeds 2 MB")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="field schema is not valid JSON") from exc
    return _validate_field_schema(payload)


def _schema_projection(schema: dict[str, Any]) -> dict[str, Any]:
    sections = schema["sections"]
    return {
        "schema_id": str(schema.get("schema_id") or "custom-schema"),
        "field_count": sum(len(section["fields"]) for section in sections),
        "section_count": len(sections),
        "sections": [str(section.get("label") or section["key"]) for section in sections],
    }


def _key(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not result or len(result) > 120:
        raise HTTPException(status_code=400, detail="project or bidder name is invalid")
    return result


def _idempotency_key(request: Request, *, required: bool = False) -> str | None:
    key = request.headers.get("idempotency-key", "").strip() or None
    if required and not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if key and len(key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return key


def _project(session: Any, project_id: str, tenant_id: str) -> WorkbenchProject:
    project = session.scalar(
        select(WorkbenchProject).where(
            WorkbenchProject.id == project_id,
            WorkbenchProject.tenant_id == tenant_id,
            WorkbenchProject.stage != "deleted",
        )
    )
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _payload_tender_version_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tender_version_id") or "").strip()


def _tender_version_has_been_replaced(version: TenderVersion | None) -> bool:
    if version is None:
        return False
    data = version.understanding_json if isinstance(version.understanding_json, dict) else {}
    return isinstance(data.get("supersedes"), dict)


def _operation_matches_current_tender(
    operation: Operation | None,
    project: WorkbenchProject,
    version: TenderVersion | None = None,
) -> bool:
    if operation is None:
        return False
    pinned = _payload_tender_version_id(getattr(operation, "payload_json", None))
    if pinned:
        return pinned == str(project.tender_version_id)
    return not _tender_version_has_been_replaced(version)


def _current_project_operation(
    session: Any,
    project: WorkbenchProject,
    *,
    stage: str,
    statuses: tuple[str, ...] | None = None,
    version: TenderVersion | None = None,
    tenant_id: str | None = None,
) -> Operation | None:
    filters = [
        Operation.entity_type == "workbench_project",
        Operation.entity_id == project.id,
        Operation.stage == stage,
    ]
    if tenant_id:
        filters.append(Operation.tenant_id == tenant_id)
    if statuses:
        filters.append(Operation.status.in_(statuses))
    for operation in session.scalars(
        select(Operation).where(*filters).order_by(Operation.created_at.desc(), Operation.id.desc())
    ):
        if _operation_matches_current_tender(operation, project, version):
            return operation
    return None


def _state(session: Any, project: WorkbenchProject) -> dict[str, Any]:
    rows = list(session.scalars(select(WorkbenchBid).where(WorkbenchBid.project_id == project.id).order_by(WorkbenchBid.created_at)))
    bids = {row.bid_id: session.get(Bid, row.bid_id) for row in rows}
    evaluations = {row.evaluation_id: session.get(Evaluation, row.evaluation_id) for row in rows if row.evaluation_id}
    evaluation_operations: dict[str, Operation] = {}
    for operation in session.scalars(
        select(Operation)
        .where(
            Operation.stage == "evaluation",
            Operation.entity_id.in_(list(evaluations) or [""]),
        )
        .order_by(Operation.created_at.desc())
    ):
        evaluation_operations.setdefault(operation.entity_id, operation)
    version = session.get(TenderVersion, project.tender_version_id)
    understanding_job = _current_project_operation(
        session, project, stage="understanding", version=version
    )
    intake_operations = list(session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "workbench_project",
            Operation.entity_id == project.id,
            Operation.stage == "intake",
        )
        .order_by(Operation.created_at.desc())
    ))
    current_bid_ids = {str(row.bid_id) for row in rows}
    has_revisions = any(bid and bid.previous_bid_id for bid in bids.values())
    intake_job = None
    for operation in intake_operations:
        payload = operation.payload_json or {}
        if payload.get("vendor_key"):
            continue
        pinned = {str(value) for value in payload.get("bid_ids") or []}
        if pinned == current_bid_ids or (not pinned and not has_revisions):
            intake_job = operation
            break
    classification_operations: dict[str, Operation] = {}
    for operation in intake_operations:
        payload = operation.payload_json or {}
        vendor_key = str(payload.get("vendor_key") or "")
        row = next((item for item in rows if item.vendor_key == vendor_key), None)
        pinned_bid_id = str(payload.get("bid_id") or "")
        current_bid = bids.get(row.bid_id) if row else None
        if vendor_key and row and (
            pinned_bid_id == str(row.bid_id)
            or (not pinned_bid_id and current_bid and not current_bid.previous_bid_id)
        ):
            classification_operations.setdefault(vendor_key, operation)
    understanding = dict(version.understanding_json or {}) if version else {}
    frozen = understanding.get("frozen_contract") or {}
    artifacts = []
    if frozen.get("criteria"):
        artifacts.append("evaluation_plan")
    if frozen.get("skipped_criteria"):
        artifacts.append("contract_limitations")
    artifacts.extend(f"manual_intake:{row.vendor_key}" for row in rows if row.intake_json)
    return {
        "project_id": project.id,
        "project_key": project.project_key,
        "title": project.title,
        "stage": project.stage,
        "tender_id": project.tender_id,
        "tender_version_id": project.tender_version_id,
        "compliance": {
            "enabled": bool((understanding.get("policy_audit") or {}).get("enabled")),
            "status": str((understanding.get("compliance") or {}).get("status") or "not_started"),
            "open": sum(
                1
                for finding in (understanding.get("compliance") or {}).get("findings") or []
                if isinstance(finding, dict)
                and (finding.get("resolution") or {}).get("status") == "open"
                and finding.get("status") in {"violation", "needs_review"}
            ),
        },
        "understanding_job": (
            {
                "job_id": understanding_job.id,
                "status": "complete" if understanding_job.status == "succeeded" else understanding_job.status,
                "error_code": understanding_job.error_code,
                "error": understanding_job.error_detail,
            }
            if understanding_job
            else None
        ),
        "intake_job": (
            {
                "job_id": intake_job.id,
                "status": "complete" if intake_job.status == "succeeded" else intake_job.status,
                "error_code": intake_job.error_code,
                "error": intake_job.error_detail,
            }
            if intake_job
            else None
        ),
        "vendors": [row.vendor_key for row in rows],
        "bidders": [
            {
                "vendor_id": row.vendor_key,
                "bidder_name": row.vendor_name,
                "bid_id": row.bid_id,
                "original_filename": next(
                    (
                        document.file_name
                        for document in session.scalars(
                            select(Document).where(Document.bid_id == row.bid_id).limit(1)
                        )
                    ),
                    "",
                ),
                "package_bytes": sum(
                    document.size_bytes
                    for document in session.scalars(select(Document).where(Document.bid_id == row.bid_id))
                ),
                "documents_prepared": bool(row.intake_json),
                "classification_status": row.classification_status,
                "classification_operation": (
                    {
                        "job_id": classification_operations[row.vendor_key].id,
                        "status": classification_operations[row.vendor_key].status,
                        "error_code": classification_operations[row.vendor_key].error_code,
                        "error": classification_operations[row.vendor_key].error_detail,
                    }
                    if row.vendor_key in classification_operations
                    else None
                ),
                "evaluation_id": row.evaluation_id,
                "evaluation_status": evaluations[row.evaluation_id].status if row.evaluation_id and evaluations.get(row.evaluation_id) else None,
                "evaluation_operation_id": (
                    evaluation_operations[row.evaluation_id].id
                    if row.evaluation_id and row.evaluation_id in evaluation_operations
                    else None
                ),
                "evaluation_error_code": (
                    evaluation_operations[row.evaluation_id].error_code
                    if row.evaluation_id and row.evaluation_id in evaluation_operations
                    else None
                ),
                "evaluation_error": (
                    evaluation_operations[row.evaluation_id].error_detail
                    if row.evaluation_id and row.evaluation_id in evaluation_operations
                    else None
                ),
            }
            for row in rows
        ],
        "artifacts": artifacts,
    }


def _understanding(session: Any, project: WorkbenchProject) -> tuple[TenderVersion, dict[str, Any]]:
    version = session.get(TenderVersion, project.tender_version_id)
    if not version:
        raise HTTPException(status_code=409, detail="project tender version is missing")
    return version, dict(version.understanding_json or {})


def _bidder(session: Any, project: WorkbenchProject, vendor_key: str) -> WorkbenchBid:
    row = session.scalar(
        select(WorkbenchBid).where(
            WorkbenchBid.project_id == project.id,
            WorkbenchBid.vendor_key == vendor_key,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="bidder not found")
    return row


def _document_item(document: Document, kind: str) -> dict[str, Any]:
    return {
        "document_id": document.id,
        "document_kind": kind,
        "file_name": document.file_name,
        "content_type": document.content_type,
        "page_count": None,
        "url": f"/v1/workbench/documents/{document.id}/content" if Path(document.file_name).suffix.lower() == ".pdf" else None,
    }


def _source_urls(
    data: dict[str, Any],
    documents: list[Document],
    project_id: str,
    base_url: str,
) -> dict[str, tuple[str, str | None]]:
    pdfs = [document for document in documents if Path(document.file_name).suffix.lower() == ".pdf"]
    by_name = {document.file_name.lower(): document for document in pdfs}
    urls: dict[str, tuple[str, str | None]] = {}
    for item in data.get("file_inventory") or []:
        source_id = str(item.get("document_id") or "")
        preview = (data.get("preview_sources") or {}).get(source_id)
        if isinstance(preview, dict) and preview.get("object_key"):
            base = f"{base_url}/v1/workbench/projects/{project_id}/sources/{source_id}"
            urls[source_id] = (
                f"{base}/content",
                f"{base}/pages/{{page}}.png" if _preview_is_pdf(preview) else None,
            )
            continue
        document = by_name.get(str(item.get("file_name") or "").lower())
        if source_id and document:
            base = f"{base_url}/v1/workbench/documents/{document.id}"
            urls[source_id] = (f"{base}/content", f"{base}/pages/{{page}}.png")
    return urls


def _ledger_with_urls(ledger: dict[str, Any], urls: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    result = copy.deepcopy(ledger)
    for entry in result.get("entries") or []:
        for evidence in entry.get("evidence") or []:
            pair = urls.get(str(evidence.get("document_id") or ""))
            if not pair:
                continue
            evidence["document_url"] = pair[0]
            evidence["pdf_url"] = pair[0]
            if pair[1] and evidence.get("page"):
                evidence["page_image_url"] = pair[1].format(page=evidence["page"])
                evidence["page_url"] = evidence["page_image_url"]
    return result


def _unlink_many(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _render_pdf_page(source: Path, image: Path, page_number: int) -> None:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(source)) as pdf:
        if page_number > len(pdf):
            raise HTTPException(status_code=404, detail="document page not found")
        page = pdf[page_number - 1]
        try:
            bitmap = page.render(scale=2)
            try:
                rendered = bitmap.to_pil()
                try:
                    rendered.save(image, format="PNG")
                finally:
                    rendered.close()
            finally:
                bitmap.close()
        finally:
            page.close()
