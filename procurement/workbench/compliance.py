"""Tender policy-compliance audit routes (BHEL Purchase/Works policy gate)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select

from procurement.api import _actor_user_id, _hash, _sessions, _store_document, _upload_sha256
from procurement.domain.models import Document, Operation, TenderVersion, WorkbenchProject
from procurement.domain.revisions import supersede_tender_version
from procurement.platform.auth import require_role
from procurement.platform.database import session_scope
from sqlalchemy.orm.attributes import flag_modified
from procurement.platform.operations import audit, enqueue, idempotent_result, remember_result
from procurement.workbench.common import _idempotency_key, _key, _project, _state, _understanding

router = APIRouter(prefix="/v1/workbench", tags=["workbench"])

_BLOCKING_FINDING_STATUSES = {"violation", "needs_review"}
_WAIVER_ACTION_MARKERS = ("waiv", "exempt", "relax", "deviation", "accept")


def _policy_audit(data: dict[str, Any]) -> dict[str, Any]:
    policy_audit = data.get("policy_audit")
    return policy_audit if isinstance(policy_audit, dict) else {}


def _compliance(data: dict[str, Any]) -> dict[str, Any]:
    compliance = data.get("compliance")
    return compliance if isinstance(compliance, dict) else {}


def compliance_gate_detail(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return a 409 detail dict when the compliance gate blocks contract work."""
    if not _policy_audit(data).get("enabled"):
        return None
    compliance = _compliance(data)
    if compliance.get("status") != "completed":
        coverage = compliance.get("coverage") if isinstance(compliance.get("coverage"), dict) else {}
        return {
            "code": (
                "TENDER_COMPLIANCE_INCOMPLETE"
                if compliance.get("status") in {"incomplete", "policy_selection_required"}
                else "TENDER_COMPLIANCE_PENDING"
            ),
            "message": (
                "Tender compliance coverage is incomplete. Review the failed coverage or select the governing policy, then run the audit again."
                if compliance.get("status") in {"incomplete", "policy_selection_required"}
                else "Tender compliance audit has not completed. Run and review it before the criteria step."
            ),
            "coverage": coverage,
        }
    coverage = compliance.get("coverage") if isinstance(compliance.get("coverage"), dict) else {}
    # v1 audits predate source-coverage accounting. Keep already-reviewed
    # legacy projects usable; every new v2 audit must prove full coverage.
    if compliance.get("schema_version") == "tender-compliance.v2" and not coverage.get("complete"):
        return {
            "code": "TENDER_COMPLIANCE_INCOMPLETE",
            "message": "Tender compliance coverage is incomplete. Every tender provision and applicable policy clause must be accounted for.",
            "coverage": coverage,
        }
    open_findings = [
        str(finding.get("finding_id"))
        for finding in compliance.get("findings") or []
        if isinstance(finding, dict)
        and (finding.get("resolution") or {}).get("status") == "open"
        and finding.get("status") in _BLOCKING_FINDING_STATUSES
    ]
    if open_findings:
        return {
            "code": "TENDER_COMPLIANCE_OPEN",
            "message": f"{len(open_findings)} compliance finding(s) are still open. Resolve or waive them before the criteria step.",
            "open_findings": open_findings,
        }
    return None


def _open_count(compliance: dict[str, Any]) -> int:
    return sum(
        1
        for finding in compliance.get("findings") or []
        if isinstance(finding, dict)
        and (finding.get("resolution") or {}).get("status") == "open"
        and finding.get("status") in _BLOCKING_FINDING_STATUSES
    )


def _waiver_approvals(finding: dict[str, Any]) -> list[dict[str, Any]]:
    from procurement.compliance.approvals import approvals_for_finding

    clause_id = str(finding.get("clause_id") or "")
    return [
        row
        for row in approvals_for_finding(finding)
        if str(row.get("clause_id") or "") == clause_id
        or any(marker in str(row.get("action") or "").casefold() for marker in _WAIVER_ACTION_MARKERS)
    ]


def _finding_views(compliance: dict[str, Any]) -> list[dict[str, Any]]:
    """Backfill the real policy clause for pre-v2 package findings without mutating history."""
    findings = [dict(item) for item in compliance.get("findings") or [] if isinstance(item, dict)]
    from procurement.compliance.approvals import approvals_for_finding

    for item in findings:
        item["approvals"] = approvals_for_finding(item)
        item["waiver_approvals"] = _waiver_approvals(item)
    legacy = [
        item for item in findings
        if str(item.get("finding_id") or "").startswith("rule-package-missing-")
        and (item.get("clause_id") == "PKG" or not item.get("clause_quote"))
    ]
    if not legacy or not compliance.get("policy_id"):
        return findings
    from procurement.compliance.corpus import load_corpus
    from procurement.compliance.rules import hosted_document_clause

    corpus = load_corpus(str(compliance["policy_id"]))
    clause = hosted_document_clause(corpus)
    if not clause:
        return findings
    for item in legacy:
        item.update({
            "clause_id": str(clause.get("clause_id") or ""),
            "clause_title": str(clause.get("title") or clause.get("section_title") or ""),
            "clause_quote": " ".join(str(clause.get("text") or "").split())[:1200],
            "policy_reference": {
                "policy_id": corpus.get("policy_id"),
                "policy_name": corpus.get("name"),
                "clause_id": str(clause.get("clause_id") or ""),
            },
            "title": "Referenced tender document is missing from the uploaded package",
            "detail": "The tender incorporates another document by reference, but that document is not present in the uploaded package. Its requirements cannot be included in the bid-evaluation criteria.",
            "required_action": "Add the referenced document, upload the corrected tender package, and run this review again.",
            "consequence": "Bid-evaluation criteria cannot be finalized until the referenced document is available or an authorized exception is recorded.",
        })
    return findings


def _titled_active_checks(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [dict(item) for item in coverage.get("active_checks") or [] if isinstance(item, dict)]
    if not checks:
        return checks
    from procurement.compliance.corpus import load_corpus

    books: dict[str, dict[str, dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for item in checks:
        policy_id = str(item.get("policy_id") or "")
        if policy_id and policy_id not in books:
            try:
                corpus = load_corpus(policy_id)
            except Exception:
                books[policy_id] = {}
                names[policy_id] = policy_id
            else:
                books[policy_id] = {
                    str(clause.get("clause_id") or ""): clause
                    for clause in corpus.get("clauses") or []
                    if isinstance(clause, dict)
                }
                names[policy_id] = str(corpus.get("name") or policy_id)
        clause = books.get(policy_id, {}).get(str(item.get("clause_id") or "")) or {}
        if not item.get("title"):
            item["title"] = str(clause.get("title") or clause.get("section_title") or "")
        if not item.get("policy_name"):
            item["policy_name"] = names.get(policy_id) or policy_id
    return checks


def _compliance_view(data: dict[str, Any]) -> dict[str, Any]:
    compliance = _compliance(data)
    policy_id = compliance.get("policy_id") or _policy_audit(data).get("policy_override")
    policy_name = compliance.get("policy_name")
    if policy_id and not policy_name:
        from procurement.compliance.corpus import load_corpus

        policy_name = load_corpus(str(policy_id)).get("name")
    coverage = dict(compliance.get("coverage") or {})
    if coverage.get("active_checks"):
        coverage["active_checks"] = _titled_active_checks(coverage)
    return {
        "enabled": bool(_policy_audit(data).get("enabled")),
        "status": compliance.get("status") or "not_started",
        "policy_id": policy_id,
        "policy_name": policy_name,
        "detection": compliance.get("detection"),
        "summary": compliance.get("summary") or {},
        "coverage": coverage,
        "open": _open_count(compliance),
        "completed_at": compliance.get("completed_at"),
    }


class PolicyOverride(BaseModel):
    policy_id: str


class FindingResolution(BaseModel):
    action: str
    note: str = ""
    approval_authority: str = ""
    approval_reference: str = ""


@router.post("/projects/{project_id}/compliance/run", status_code=status.HTTP_202_ACCEPTED)
def run_compliance(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        if not _policy_audit(data).get("enabled"):
            raise HTTPException(status_code=409, detail="policy compliance audit is not enabled for this project")
        if project.stage not in {"rfp_understood", "contract_drafted"}:
            raise HTTPException(status_code=409, detail="tender understanding must complete before the compliance audit")
        existing = session.scalar(
            select(Operation)
            .where(
                Operation.tenant_id == principal.tenant_id,
                Operation.entity_type == "workbench_project",
                Operation.entity_id == project.id,
                Operation.stage == "compliance",
                Operation.status.in_(("queued", "running")),
            )
            .order_by(Operation.created_at.desc())
        )
        if existing:
            return {"job_id": existing.id, "status": existing.status}
        compliance = _compliance(data)
        compliance["status"] = "queued"
        data["compliance"] = compliance
        version.understanding_json = data
        flag_modified(version, "understanding_json")
        operation = enqueue(
            session,
            tenant_id=principal.tenant_id,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="compliance",
            payload={
                "project_id": project.id,
                "tender_version_id": version.id,
                "policy_id": _policy_audit(data).get("policy_override") or "",
            },
            key=_idempotency_key(request, required=True),
            request_hash=_hash({
                "action": "workbench.compliance.run",
                "project_id": project.id,
                "tender_version_id": version.id,
                "policy_id": _policy_audit(data).get("policy_override") or "auto",
                "schema_version": "tender-compliance.v2",
            }),
        )
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.compliance.queued",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=None,
            before={"stage": project.stage},
            after={"operation_id": operation.id},
        )
        return {"job_id": operation.id, "status": operation.status}


@router.get("/projects/{project_id}/compliance")
def get_compliance(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    from procurement.compliance.corpus import available_policies

    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        _version, data = _understanding(session, project)
        return {
            **_compliance_view(data),
            "policy_override": _policy_audit(data).get("policy_override") or "",
            "available_policies": available_policies(),
            "findings": _finding_views(_compliance(data)),
            "line_coverage": _compliance(data).get("line_coverage") or [],
            "clause_coverage": _compliance(data).get("clause_coverage") or [],
        }


@router.post("/projects/{project_id}/compliance/policy")
def override_policy(project_id: str, body: PolicyOverride, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer")
    from procurement.compliance.corpus import available_policies

    known = {policy["policy_id"] for policy in available_policies()}
    if body.policy_id not in known:
        raise HTTPException(status_code=422, detail=f"unknown policy_id {body.policy_id!r}")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        compliance = _compliance(data)
        if compliance.get("status") in {"queued", "in_progress"}:
            raise HTTPException(status_code=409, detail="compliance audit is running; policy cannot be changed now")
        policy_audit = _policy_audit(data)
        policy_audit["enabled"] = True
        policy_audit["policy_override"] = body.policy_id
        data["policy_audit"] = policy_audit
        if compliance:
            history = data.get("compliance_history")
            history = list(history) if isinstance(history, list) else []
            history.append(compliance)
            data["compliance_history"] = history
        data["compliance"] = {"status": "not_started"}
        version.understanding_json = data
        flag_modified(version, "understanding_json")
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.compliance.policy_override",
            entity_type="tender_version",
            entity_id=version.id,
            reason=None,
            before={"policy_id": compliance.get("policy_id"), "status": compliance.get("status")},
            after={"policy_id": body.policy_id},
        )
        return _compliance_view(data)


@router.post("/projects/{project_id}/compliance/findings/{finding_id}/resolve")
def resolve_finding(project_id: str, finding_id: str, body: FindingResolution, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "approver")
    action = body.action.strip().lower()
    if action not in {"resolve", "waive", "reopen"}:
        raise HTTPException(status_code=422, detail="action must be resolve, waive, or reopen")
    if action == "waive" and not body.note.strip():
        raise HTTPException(status_code=422, detail="waiving a finding requires a justification note")
    with session_scope(_sessions()) as session:
        actor_id = _actor_user_id(session, principal)
        project = _project(session, project_id, principal.tenant_id)
        version, data = _understanding(session, project)
        compliance = _compliance(data)
        findings = compliance.get("findings") or []
        target = next(
            (finding for finding in findings if str(finding.get("finding_id")) == finding_id),
            None,
        )
        if not target:
            raise HTTPException(status_code=404, detail="finding not found")
        approval: dict[str, Any] | None = None
        if action == "waive":
            allowed = _waiver_approvals(target)
            if not allowed:
                raise HTTPException(
                    status_code=422,
                    detail="the governing policy does not define an approval path for waiving this finding",
                )
            authority = body.approval_authority.strip()
            reference = body.approval_reference.strip()
            if not authority or not reference:
                raise HTTPException(
                    status_code=422,
                    detail="an approval authority and approval reference are required for a policy waiver",
                )
            selected = next(
                (
                    row
                    for row in allowed
                    if str(row.get("authority_title") or "").casefold() == authority.casefold()
                ),
                None,
            )
            if selected is None:
                raise HTTPException(
                    status_code=422,
                    detail="the selected approval authority is not allowed by the governing policy clause",
                )
            approval = {**selected, "reference": reference}
        before = dict(target.get("resolution") or {})
        target["resolution"] = {
            "status": {"resolve": "resolved", "waive": "waived", "reopen": "open"}[action],
            "note": body.note.strip(),
            "resolved_by": principal.subject,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        if approval:
            target["resolution"]["approval"] = approval
        compliance["findings"] = findings
        summary = dict(compliance.get("summary") or {})
        summary["open"] = _open_count(compliance)
        compliance["summary"] = summary
        data["compliance"] = compliance
        version.understanding_json = data
        flag_modified(version, "understanding_json")
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type=f"workbench.compliance.finding.{action}d",
            entity_type="tender_version",
            entity_id=version.id,
            reason=body.note.strip() or None,
            before={"finding_id": finding_id, "resolution": before},
            after={"finding_id": finding_id, "resolution": target["resolution"]},
        )
        return {"finding": target, "summary": summary, "open": summary["open"]}


@router.post("/projects/{project_id}/tender/reupload", status_code=status.HTTP_201_CREATED)
async def reupload_tender(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    reason: str | None = Form(None),
) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer")
    idempotency_key = _idempotency_key(request, required=True)
    request_hash = _hash({
        "action": "workbench.tender.reupload",
        "project_id": project_id,
        "reason": (reason or "").strip(),
        "file_sha256": _upload_sha256(file),
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
        project = session.scalar(
            select(WorkbenchProject)
            .where(
                WorkbenchProject.id == project_id,
                WorkbenchProject.tenant_id == principal.tenant_id,
            )
            .with_for_update()
        )
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        if project.stage not in {"tender_uploaded", "rfp_understood", "contract_drafted"}:
            raise HTTPException(
                status_code=409,
                detail="tender can only be re-uploaded before the contract is frozen",
            )
        if session.scalar(
            select(Operation.id).where(
                Operation.tenant_id == principal.tenant_id,
                Operation.entity_type == "workbench_project",
                Operation.entity_id == project.id,
                Operation.status.in_(("queued", "running")),
            ).limit(1)
        ):
            raise HTTPException(status_code=409, detail="wait for the active project operation before replacing the tender")
        version, data = _understanding(session, project)
        version = session.scalar(
            select(TenderVersion)
            .where(TenderVersion.id == version.id)
            .with_for_update()
        )
        if not version:
            raise HTTPException(status_code=409, detail="project tender version is missing")
        old_documents = list(
            session.scalars(
                select(Document).where(
                    Document.tender_version_id == version.id,
                    Document.tenant_id == principal.tenant_id,
                )
            )
        )
        old_document_ids = [document.id for document in old_documents]
        for document in old_documents:
            # Preserve the uploaded object, tender relationship, and extracted
            # pages; active-tender queries exclude this explicit history marker.
            document.source_kind = "superseded_upload"
            document.extraction_status = "superseded"
        new_version = supersede_tender_version(
            version,
            {
                "field_schema": data.get("field_schema"),
                "policy_audit": _policy_audit(data),
                "supersedes": {
                    "tender_version_id": version.id,
                    "document_ids": old_document_ids,
                },
            },
        )
        session.add(new_version)
        session.flush([new_version])
        document = await _store_document(
            session,
            file,
            tenant_id=principal.tenant_id,
            actor_id=actor_id,
            tender_id=project.tender_id,
            tender_version_id=new_version.id,
            bid_id=None,
        )
        before = {"stage": project.stage, "tender_version_id": project.tender_version_id}
        project.tender_version_id = new_version.id
        project.stage = "tender_uploaded"
        project.updated_at = datetime.now(timezone.utc)
        audit(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=actor_id,
            event_type="workbench.tender.reuploaded",
            entity_type="workbench_project",
            entity_id=project.id,
            reason=(reason or "").strip() or None,
            before=before,
            after={
                "stage": "tender_uploaded",
                "tender_version_id": new_version.id,
                "document_id": document.id,
                "replaced_documents": len(old_document_ids),
            },
        )
        result = _state(session, project)
        remember_result(
            session,
            tenant_id=principal.tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            entity_type="workbench_project",
            entity_id=project.id,
            stage="tender_reupload",
            result=result,
        )
        return result


@router.get("/projects/{project_id}/tender/revisions")
def tender_revisions(project_id: str, request: Request) -> dict[str, Any]:
    principal = require_role(request, "admin", "procurement_officer", "reviewer", "auditor")
    with session_scope(_sessions()) as session:
        project = _project(session, project_id, principal.tenant_id)
        versions = list(
            session.scalars(
                select(TenderVersion)
                .where(TenderVersion.tender_id == project.tender_id)
                .order_by(TenderVersion.version_number.desc())
            )
        )
        documents = list(
            session.scalars(
                select(Document)
                .where(
                    Document.tender_id == project.tender_id,
                    Document.tenant_id == principal.tenant_id,
                )
                .order_by(Document.created_at, Document.id)
            )
        )
        by_version: dict[str, list[Document]] = {}
        for document in documents:
            if document.tender_version_id:
                by_version.setdefault(document.tender_version_id, []).append(document)
        return {
            "tender_id": project.tender_id,
            "current_tender_version_id": project.tender_version_id,
            "revisions": [
                {
                    "tender_version_id": version.id,
                    "previous_version_id": version.previous_version_id,
                    "version_number": version.version_number,
                    "status": version.status,
                    "current": version.id == project.tender_version_id,
                    "documents": [
                        {
                            "document_id": document.id,
                            "file_name": document.file_name,
                            "sha256": document.sha256,
                            "size_bytes": document.size_bytes,
                        }
                        for document in by_version.get(version.id, [])
                    ],
                    "created_at": version.created_at.isoformat() if version.created_at else None,
                    "superseded_at": version.superseded_at.isoformat() if version.superseded_at else None,
                }
                for version in versions
            ],
        }
