"""Bid-evaluation pipeline for frozen tender requirements and bidder evidence."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from procurement.domain.schemas import (
    Criterion,
    DocumentRecord,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    RequirementMatrix,
    RequirementMatrixItem,
    TenderProfile,
)
from procurement.domain.serialization import model_dump as _default_model_dump
from procurement.evaluation.assessment import decision_agent
from procurement.evaluation.contracts import normalize_criterion
from procurement.evaluation.criteria import normalize_prohibition_criterion
from procurement.evaluation.document_verifier import document_verifier_agent as _default_document_verifier_agent
from procurement.evaluation.evidence_pipeline import (
    cross_document_reconciliation_agent as _default_cross_document_reconciliation_agent,
    evidence_ledger_builder as _default_evidence_ledger_builder,
    evidence_packet_builder_agent as _default_evidence_packet_builder_agent,
    specialist_orchestrator_agent as _default_specialist_orchestrator_agent,
)
from procurement.evaluation.evidence_scout import (
    evidence_mapper_agent as _default_evidence_mapper_agent,
    grouped_evidence_scout_agent as _default_grouped_evidence_scout_agent,
)
from procurement.evaluation.index import (
    artifact_labels_and_capabilities,
    infer_proof_contract,
)
from procurement.evaluation.ocr_recovery import run_ocr_candidates as _default_run_ocr_candidates
from procurement.evaluation.pipeline import run_criteria_evaluation_stage as _default_run_criteria_evaluation_stage
from procurement.evaluation.planning import normalize_requirement_checks, resolved_evidence_type
from procurement.evaluation.semantic_retrieval import bidder_semantic_index
from procurement.evaluation.validation import enforce_server_validation as _default_enforce_server_validation
from procurement.evaluation.vendor_profile import extract_vendor_profile as _default_extract_vendor_profile
from procurement.platform.ingestion import unpack_upload
from procurement.platform.job_context import (
    check_job_deadline as _default_check_job_deadline,
    llm_usage_summary as _default_llm_usage_summary,
    reset_llm_usage as _default_reset_llm_usage,
)
from procurement.understanding.service import (
    evidence_for_reviewed_atomic_field,
    evidence_groups_for_reviewed_atomic_field,
    evidence_requirements_for_field,
    requirement_semantics_for_reviewed_atomic_field,
)

from .support import (
    _bid_tender_match,
    _canonical_evidence_groups,
    _is_price_document_type,
    _preserve_ocr_uncertainty,
    _profile_data_from_understanding,
    _raise_on_ocr_errors,
    commercial_quarantine,
)

logger = logging.getLogger("procurement.worker")


@dataclass(frozen=True)
class EvaluationHooks:
    """Optional boundary callbacks retained for tests and compatibility callers."""

    document_verifier_agent: Callable[..., Any] | None = None
    run_ocr_candidates: Callable[..., Any] | None = None
    run_criteria_evaluation_stage: Callable[..., Any] | None = None
    extract_vendor_profile: Callable[..., Any] | None = None
    grouped_evidence_scout_agent: Callable[..., Any] | None = None
    evidence_mapper_agent: Callable[..., Any] | None = None
    evidence_packet_builder_agent: Callable[..., Any] | None = None
    evidence_ledger_builder: Callable[..., Any] | None = None
    specialist_orchestrator_agent: Callable[..., Any] | None = None
    cross_document_reconciliation_agent: Callable[..., Any] | None = None
    enforce_server_validation: Callable[..., Any] | None = None
    check_job_deadline: Callable[..., Any] | None = None
    reset_llm_usage: Callable[..., Any] | None = None
    llm_usage_summary: Callable[..., Any] | None = None
    model_dump: Callable[..., Any] | None = None


def approved_requirement_matrix(understanding: dict[str, Any]):
    """Build the exact matrix authorized by the frozen human-reviewed checklist."""
    matrix = RequirementMatrix.model_validate(understanding.get("requirement_matrix") or {})
    base_by_id = {item.criterion_id: item for item in matrix.items}
    approved_items: list[RequirementMatrixItem] = []
    frozen_by_id: dict[str, dict[str, Any]] = {}
    frozen_contract = understanding.get("frozen_contract") or {}
    matrix.extensions["unautomated_requirements"] = list(frozen_contract.get("unautomated_requirements") or [])
    for criterion_id, reasons in (frozen_contract.get("skipped_criteria") or {}).items():
        source_item = base_by_id.get(criterion_id)
        matrix.extensions["unautomated_requirements"].append({
            "criterion_id": criterion_id,
            "requirement": source_item.requirement if source_item else criterion_id,
            "reason": "Checklist criterion skipped with unresolved fields: " + "; ".join(str(reason) for reason in reasons),
        })
    for frozen in frozen_contract.get("criteria") or []:
        if not isinstance(frozen, dict):
            continue
        criterion_id = str(frozen.get("criterion_id") or "").strip()
        if not criterion_id or (frozen.get("review") or {}).get("included") is False:
            continue
        frozen = normalize_criterion(frozen)
        frozen_by_id[criterion_id] = frozen
        rule = frozen.get("rule") if isinstance(frozen.get("rule"), dict) else {}
        base = base_by_id.get(criterion_id)
        required_documents = [
            str(name).strip()
            for name in rule.get("required_documents") or []
            if str(name).strip()
        ]
        if base:
            item = RequirementMatrixItem.model_validate(base.model_dump())
        else:
            item = RequirementMatrixItem(
                criterion_id=criterion_id,
                requirement=str(rule.get("requirement") or frozen.get("title") or "Human-approved requirement"),
                category=str(frozen.get("family") or "other"),
                stage="bid_evaluation",
            )
        item.requirement = str(rule.get("requirement") or frozen.get("title") or item.requirement)
        item.category = str(frozen.get("family") or rule.get("rule_family") or item.category or "other")
        item.mandatory = bool(rule.get("mandatory", item.mandatory))
        item.failure_effect = str(rule.get("failure_effect") or item.failure_effect)
        item.pass_rule = str(rule.get("pass_rule") or item.pass_rule)
        item.criterion_kind = str(frozen.get("criterion_kind") or "gate")
        item.evaluation_scope = str(frozen.get("evaluation_scope") or "technical_qualification")
        item.materiality = str(frozen.get("materiality") or "mandatory_blocking")
        item.applicability = dict(frozen.get("applicability") or {"state": "applicable"})
        item.decision_effect = str(frozen.get("decision_effect") or item.failure_effect)
        item.semantic_key = str(frozen.get("semantic_key") or "")
        item.threshold = dict(frozen.get("threshold") or {})
        item.evidence_groups = list(frozen.get("evidence_groups") or [])
        item.depends_on = list(frozen.get("depends_on") or [])
        item.blocks = list(frozen.get("blocks") or [])
        item.source_references = list(frozen.get("source_refs") or frozen.get("source_references") or [])
        base_document_names = [evidence.document_name for evidence in item.required_evidence]
        documents_changed = (
            "required_documents" in rule
            and (not base or required_documents != base_document_names)
        )
        frozen_evidence = frozen.get("required_evidence")
        if (isinstance(frozen_evidence, list) and frozen_evidence
                and [proof.get("document_name") for proof in frozen_evidence] == required_documents):
            item.required_evidence = [EvidenceRequirement.model_validate(proof) for proof in frozen_evidence]
        elif documents_changed:
            item.required_evidence = [
                EvidenceRequirement(
                    evidence_id=f"{criterion_id}-E{index}",
                    document_name=name,
                    document_type="unknown",
                    required=True,
                    checks=["approved_checklist_requirement"],
                    acceptance_rule=item.pass_rule,
                    missing_effect="reject" if item.mandatory else "review",
                )
                for index, name in enumerate(required_documents, start=1)
            ]
        source = next(iter(frozen.get("rfp_source_refs") or []), {})
        if not item.source_quote and isinstance(source, dict):
            item.source_quote = str(source.get("quote") or "") or None
        item.extensions = {
            **dict(item.extensions or {}),
            **dict(frozen.get("extensions") or {}),
            "approved_checklist": {
                "revision": int(frozen_contract.get("revision") or 0),
                "origin": str((frozen.get("review") or {}).get("origin") or "tender_derived"),
                "materiality": str(frozen.get("materiality") or ""),
                "evaluation_scope": str(frozen.get("evaluation_scope") or "technical_qualification"),
                "required_documents_changed": documents_changed,
            },
        }
        if documents_changed:
            evaluation = item.extensions.get("evaluation")
            evaluation = dict(evaluation) if isinstance(evaluation, dict) else {}
            evaluation["evidence_groups"] = [
                [evidence.evidence_id] for evidence in item.required_evidence if evidence.required
            ]
            item.evidence_groups = _canonical_evidence_groups(evaluation["evidence_groups"])
            item.extensions["evaluation"] = evaluation
        reviewed_field = (item.extensions or {}).get("reviewed_field")
        reviewed_field = reviewed_field if isinstance(reviewed_field, dict) else {}
        generated_evaluation = (item.extensions or {}).get("evaluation")
        generated_evaluation = generated_evaluation if isinstance(generated_evaluation, dict) else {}
        if (
            item.criterion_id.startswith("FIELD-")
            and str(reviewed_field.get("atomic_value") or "").strip()
            and str(generated_evaluation.get("evaluation_scope") or "")
            == str(frozen.get("evaluation_scope") or item.evaluation_scope)
        ):
            refreshed = requirement_semantics_for_reviewed_atomic_field(
                str(reviewed_field.get("field_key") or ""),
                str(reviewed_field.get("atomic_value") or item.requirement),
                list(item.source_references or []),
                conditions=[str(value) for value in reviewed_field.get("conditions") or []],
                exceptions=[str(value) for value in reviewed_field.get("exceptions") or []],
            )
            if refreshed["evaluation_scope"] in {"post_award", "delivery_or_inspection"}:
                item.stage = refreshed["evaluation_scope"]
                item.evaluation_scope = "post_award"
                item.criterion_kind = "obligation"
                item.materiality = "post_award"
                item.mandatory = False
                item.failure_effect = "contract_remedy"
                item.decision_effect = "contract_remedy"
                item.required_evidence = []
                item.evidence_groups = []
                generated_evaluation.update(refreshed)
                generated_evaluation["evidence_groups"] = []
                item.extensions["evaluation"] = generated_evaluation
                canonical = item.extensions.get("canonical_contract")
                if isinstance(canonical, dict):
                    canonical.update(
                        {
                            "criterion_kind": "obligation",
                            "evaluation_scope": "post_award",
                            "materiality": "post_award",
                            "decision_effect": "contract_remedy",
                            "evidence_groups": [],
                        }
                    )
        if item.evaluation_scope not in {
            "post_award",
            "delivery_or_inspection",
            "commercial_out_of_scope",
        }:
            approved_items.append(item)
    matrix.items = approved_items
    if not matrix.items:
        raise ValueError("frozen reviewed requirement matrix is missing")
    return matrix, frozen_by_id


def restore_bidder_index_labels(logical_documents: list[Any], documents: list[Any]) -> None:
    """Rebuild derived routing labels when evaluating older approved classifications."""
    page_by_id = {
        page.page_id: page
        for document in documents
        for page in document.pages
    }
    for logical_document in logical_documents:
        labels, capabilities = artifact_labels_and_capabilities(
            document_type=logical_document.document_type,
            title=logical_document.title,
            summary=logical_document.content_summary,
            key_facts=logical_document.key_facts,
            text="\n".join(
                page_by_id[page_id].text
                for page_id in logical_document.page_refs
                if page_id in page_by_id
            ),
        )
        logical_document.labels = sorted(set(logical_document.labels or []) | set(labels))
        logical_document.capabilities = sorted(
            set(logical_document.capabilities or []) | set(capabilities)
        )
    for page in page_by_id.values():
        if not page.classification:
            continue
        labels, capabilities = artifact_labels_and_capabilities(
            document_type=page.classification.document_type,
            title=page.classification.title or page.file_name,
            key_facts=page.classification.key_facts,
            text=page.text,
        )
        page.classification.labels = sorted(
            set(page.classification.labels or []) | set(labels)
        )
        page.classification.capabilities = sorted(
            set(page.classification.capabilities or []) | set(capabilities)
        )


def evaluate_reviewed_bid(
    evaluation_id: str,
    bid_bundle: Path,
    work: Path,
    understanding: dict,
    intake: dict,
    progress: Callable[[str, dict[str, float]], None] | None = None,
    hooks: EvaluationHooks | None = None,
) -> tuple:
    """Evaluate approved bidder documents against the frozen reviewed contract."""
    hooks = hooks or EvaluationHooks()
    document_verifier_agent = hooks.document_verifier_agent or _default_document_verifier_agent
    run_ocr_candidates = hooks.run_ocr_candidates or _default_run_ocr_candidates
    run_criteria_evaluation_stage = hooks.run_criteria_evaluation_stage or _default_run_criteria_evaluation_stage
    extract_vendor_profile = hooks.extract_vendor_profile or _default_extract_vendor_profile
    grouped_evidence_scout_agent = hooks.grouped_evidence_scout_agent or _default_grouped_evidence_scout_agent
    evidence_mapper_agent = hooks.evidence_mapper_agent or _default_evidence_mapper_agent
    evidence_packet_builder_agent = hooks.evidence_packet_builder_agent or _default_evidence_packet_builder_agent
    evidence_ledger_builder = hooks.evidence_ledger_builder or _default_evidence_ledger_builder
    specialist_orchestrator_agent = hooks.specialist_orchestrator_agent or _default_specialist_orchestrator_agent
    cross_document_reconciliation_agent = hooks.cross_document_reconciliation_agent or _default_cross_document_reconciliation_agent
    enforce_server_validation = hooks.enforce_server_validation or _default_enforce_server_validation
    check_job_deadline = hooks.check_job_deadline or _default_check_job_deadline
    reset_llm_usage = hooks.reset_llm_usage or _default_reset_llm_usage
    llm_usage_summary = hooks.llm_usage_summary or _default_llm_usage_summary
    model_dump = hooks.model_dump or _default_model_dump
    tender_match = intake.get("tender_match") or {}
    if tender_match.get("status") == "mismatch":
        observed = ", ".join(tender_match.get("observed_tender_ids") or []) or "another tender"
        raise ValueError(
            f"bid package does not match the selected tender; observed {observed}"
        )
    reset_llm_usage(evaluation_id)
    phase_started = time.perf_counter()
    phase_seconds: dict[str, float] = {}

    def mark(stage: str) -> None:
        if progress:
            progress(stage, dict(phase_seconds))
    inventory_items = list(intake.get("documents") or [])
    inventory = {str(item.get("document_id") or ""): item for item in inventory_items}
    pages_by_document: dict[str, list[PageUnit]] = {}
    for item in intake.get("pages") or []:
        page = PageUnit.model_validate(item)
        pages_by_document.setdefault(page.document_id, []).append(page)
    sources = unpack_upload(bid_bundle, work / "vendor-sources")
    if len(sources) != len(inventory_items):
        raise ValueError("approved bidder inventory no longer matches the uploaded package")
    documents = []
    for saved, source in zip(inventory_items, sources):
        record = DocumentRecord.model_validate(saved)
        pages = sorted(pages_by_document.get(record.document_id, []), key=lambda page: page.page_number)
        documents.append(
            ExtractedDocument(
                record,
                "\n\n".join(page.text for page in pages if page.text),
                pages,
                source,
            )
        )
    effective_tender_match = _bid_tender_match(understanding, documents)
    if effective_tender_match.get("status") == "mismatch":
        observed = ", ".join(effective_tender_match.get("observed_tender_ids") or [])
        raise ValueError(
            f"bid package does not match the selected tender; observed {observed}"
        )
    commercial_open = os.getenv("COMMERCIAL_OPENING_ENABLED", "0").strip() == "1"
    quarantine = commercial_quarantine(intake)
    approved_documents = []
    for document in documents:
        document_id = document.record.document_id
        if commercial_open:
            approved_documents.append(document)
            continue
        if document_id in quarantine["drop_document_ids"]:
            continue
        if document_id not in quarantine["keep_document_ids"]:
            continue
        if quarantine["drop_page_ids"]:
            document.pages = [
                page
                for page in document.pages
                if f"{document_id}-p{page.page_number}" not in quarantine["drop_page_ids"]
            ]
            document.text = "\n\n".join(page.text for page in document.pages if page.text)
        approved_documents.append(document)
    if not approved_documents:
        raise ValueError(
            "This bid was classified as price-only. Move technical sections off Price Bid, then evaluate again."
        )
    approved_ids = {document.record.document_id for document in approved_documents}
    phase_seconds["hydrate_approved_bidder_pages"] = round(time.perf_counter() - phase_started, 3)

    classification = intake.get("classification") or {}
    reviewed = classification.get("reviewed_logical_documents")
    logical_payload = (
        reviewed
        if isinstance(reviewed, list)
        else classification.get("logical_documents") or []
    )
    logical_documents = []
    if isinstance(reviewed, list):
        for item in logical_payload:
            if not commercial_open and _is_price_document_type(str(item.get("document_type") or "")):
                continue
            if not commercial_open and str(item.get("logical_document_id") or "") in quarantine["drop_logical_ids"]:
                continue
            source_id = str(item.get("source_document_id") or "")
            if source_id not in approved_ids:
                continue
            source_record = inventory.get(source_id) or {}
            source_classification = source_record.get("classification") or {}
            content_role = str(
                item.get("content_role")
                or source_classification.get("content_role")
                or "unknown"
            )
            if content_role not in {"tender_source", "vendor_submission", "reference", "unknown"}:
                content_role = "unknown"
            input_role = str(item.get("input_role") or source_record.get("input_role") or "vendor_submission")
            if input_role not in {"tender_packet", "evaluation_guideline", "vendor_submission"}:
                input_role = "vendor_submission"
            start = int(item.get("start_page") or 1)
            end = int(item.get("end_page") or start)
            logical_documents.append(
                LogicalDocument(
                    logical_document_id=str(
                        item.get("logical_document_id")
                        or f"{source_id}-reviewed-{start}-{end}"
                    ),
                    upload_side="vendor",
                    title=str(item.get("title") or "Bid document"),
                    document_type=str(item.get("document_type") or "other"),
                    content_role=content_role,
                    input_role=input_role,
                    source_document_ids=[source_id],
                    source_files=[
                        str((inventory.get(source_id) or {}).get("file_name") or "")
                    ],
                    page_refs=[f"{source_id}-p{page}" for page in range(start, end + 1)],
                    content_summary=str(item.get("content_summary") or ""),
                    key_facts=[
                        str(fact)
                        for fact in item.get("key_facts") or []
                        if str(fact).strip()
                    ],
                    confidence=float(item.get("confidence") or 1),
                    status="reconstructed",
                )
            )
    else:
        for item in logical_payload:
            if not commercial_open and _is_price_document_type(str(item.get("document_type") or "")):
                continue
            if not commercial_open and str(item.get("logical_document_id") or "") in quarantine["drop_logical_ids"]:
                continue
            source_ids = [
                str(source_id)
                for source_id in item.get("source_document_ids") or []
                if str(source_id) in approved_ids
            ]
            if not source_ids:
                continue
            payload = dict(item)
            payload["source_document_ids"] = source_ids
            payload["page_refs"] = [
                str(page_id)
                for page_id in item.get("page_refs") or []
                if any(str(page_id).startswith(f"{source_id}-p") for source_id in source_ids)
            ]
            logical_documents.append(LogicalDocument.model_validate(payload))
    if not logical_documents:
        raise ValueError("approved bidder classification contains no technical documents")
    restore_bidder_index_labels(logical_documents, approved_documents)

    matrix, frozen_by_id = approved_requirement_matrix(understanding)
    frozen_ids = {item.criterion_id for item in matrix.items}
    for item in matrix.items:
        frozen = frozen_by_id.get(item.criterion_id) or {}
        reviewed_documents = (frozen.get("rule") or {}).get("required_documents") or []
        reviewed_field = (item.extensions or {}).get("reviewed_field")
        reviewed_field = reviewed_field if isinstance(reviewed_field, dict) else {}
        approved_checklist = (item.extensions or {}).get("approved_checklist")
        approved_checklist = approved_checklist if isinstance(approved_checklist, dict) else {}
        documents_changed = bool(approved_checklist.get("required_documents_changed"))
        is_atomic = bool(str(reviewed_field.get("atomic_value") or "").strip())
        if item.criterion_id.startswith("FIELD-") and not reviewed_documents and not is_atomic:
            field_key = str(
                reviewed_field.get("field_key")
                or item.criterion_id.removeprefix("FIELD-")
            )
            title = item.requirement.split(":", 1)[0].strip() or field_key.replace("_", " ")
            item.required_evidence = [
                EvidenceRequirement.model_validate(payload)
                for payload in evidence_requirements_for_field(
                    field_key,
                    title,
                    [evidence.document_name for evidence in item.required_evidence],
                )
            ]
        generated_placeholder = bool(item.required_evidence) and all(
            any(
                marker in evidence.document_name.casefold()
                for marker in ("supporting this tender requirement", "bidder evidence for")
            )
            for evidence in item.required_evidence
        )
        if item.criterion_id.startswith("FIELD-") and not documents_changed and (
            is_atomic or generated_placeholder or not reviewed_documents
        ):
            field_key = str(
                reviewed_field.get("field_key")
                or item.criterion_id.removeprefix("FIELD-").split("-", 1)[0]
            )
            atomic_value = str(reviewed_field.get("atomic_value") or item.requirement)
            source_documents = [
                str(value).strip()
                for value in (
                    reviewed_field.get("required_documents")
                    or reviewed_documents
                    or [evidence.document_name for evidence in item.required_evidence]
                )
                if str(value).strip()
            ]
            recovered = evidence_for_reviewed_atomic_field(
                field_key,
                item.requirement.split(":", 1)[0].strip() or field_key.replace("_", " "),
                source_documents,
                atomic_value,
                list(item.source_references or []),
            )
            if recovered:
                item.required_evidence = [
                    EvidenceRequirement.model_validate({
                        **payload,
                        "evidence_id": f"{item.criterion_id}-E{index}",
                    })
                    for index, payload in enumerate(recovered, start=1)
                ]
                runtime_groups = evidence_groups_for_reviewed_atomic_field(
                    field_key,
                    atomic_value,
                    item.pass_rule,
                    [evidence.model_dump() for evidence in item.required_evidence],
                )
                item.evidence_groups = _canonical_evidence_groups(runtime_groups)
                evaluation = (item.extensions or {}).get("evaluation")
                evaluation = dict(evaluation) if isinstance(evaluation, dict) else {}
                evaluation["evidence_groups"] = runtime_groups
                item.extensions["evaluation"] = evaluation
                canonical = item.extensions.get("canonical_contract")
                if isinstance(canonical, dict):
                    canonical["evidence_groups"] = list(item.evidence_groups)
        normalize_prohibition_criterion(item)
        if not item.required_evidence and item.evaluation_scope not in {
            "post_award",
            "commercial_out_of_scope",
            "delivery_or_inspection",
        }:
            item.required_evidence = [
                EvidenceRequirement(
                    evidence_id=f"{item.criterion_id}-E1",
                    document_name="Bidder response supporting this tender requirement",
                    document_type="unknown",
                    required=True,
                    checks=["tender_requirement_match"],
                )
            ]
        for evidence in item.required_evidence:
            evidence.document_type = resolved_evidence_type(item, evidence)
            proof_contract = dict(evidence.proof_contract or {})
            if not proof_contract:
                # Backward compatibility for checklists frozen before proof
                # contracts were part of source extraction. New checklists
                # carry the source-grounded contract and bypass inference.
                proof_contract = infer_proof_contract(evidence.model_dump(), item.model_dump())
            evidence.proof_contract = proof_contract
            if not evidence.accepted_document_types:
                evidence.accepted_document_types = list(
                    proof_contract.get("accepted_artifact_labels") or []
                )
            if not evidence.entity_roles:
                evidence.entity_roles = list(proof_contract.get("entity_roles") or [])
            if not evidence.issuer_roles:
                evidence.issuer_roles = list(proof_contract.get("issuer_roles") or [])
            if not evidence.visual_checks:
                evidence.visual_checks = list(proof_contract.get("visual_checks") or [])
    profile_data = _profile_data_from_understanding(understanding)
    governing_policy_id = str(profile_data.get("governing_policy_id") or "")
    if governing_policy_id:
        for item in matrix.items:
            item.extensions = {**dict(item.extensions or {}), "governing_policy_id": governing_policy_id}
    criteria = []
    allowed_categories = {
        "eligibility", "technical", "commercial", "financial", "compliance", "other"
    }
    for item in matrix.items:
        frozen = frozen_by_id.get(item.criterion_id) or {}
        source = next(iter(frozen.get("rfp_source_refs") or []), {})
        category = item.category if item.category in allowed_categories else "other"
        criteria.append(
            Criterion(
                criterion_id=item.criterion_id,
                category=category,
                requirement=item.requirement,
                stage=item.stage,
                failure_effect=item.failure_effect,
                mandatory=item.mandatory,
                clarifiable=bool(frozen.get("needs_human_input", item.clarifiable)),
                evaluation_scope=item.evaluation_scope,
                priority=item.criterion_kind,
                criterion_kind=item.criterion_kind,
                materiality=item.materiality,
                applicability=dict(item.applicability or {}),
                decision_effect=item.decision_effect,
                semantic_key=item.semantic_key,
                evidence_groups=list(item.evidence_groups or []),
                depends_on=list(item.depends_on or []),
                blocks=list(item.blocks or []),
                source_references=list(item.source_references or []),
                pass_condition=item.pass_rule,
                required_documents=[
                    evidence.document_name for evidence in item.required_evidence
                ],
                source_document_id=str(source.get("document_id") or "") or None,
                source_page_number=source.get("page"),
                source_quote=item.source_quote or source.get("quote"),
                confidence=1,
                extensions=dict(item.extensions or {}),
            )
        )
    profile_data["criteria"] = [model_dump(item) for item in criteria]
    tender_profile = TenderProfile.model_validate(profile_data)
    criteria_by_id = {criterion.criterion_id: criterion for criterion in tender_profile.criteria}
    for item in matrix.items:
        criterion = criteria_by_id.get(item.criterion_id)
        if criterion:
            normalize_requirement_checks(criterion, item)

    check_job_deadline()
    phase_started = time.perf_counter()
    scanned_pages = [
        (100, document, page, "bidder page requires OCR", page.page_id)
        for document in approved_documents
        for page in document.pages
        if page.ocr_required
    ]
    bidder_ocr_notes = run_ocr_candidates(
        scanned_pages,
        len(scanned_pages),
        "paddle_bidder_ocr",
    )
    bidder_ocr_errors = [
        note for note in bidder_ocr_notes if note.get("status") == "error"
    ]
    if bidder_ocr_errors:
        logger.warning(
            "Bidder evaluation OCR incomplete evaluation_id=%s failed_pages=%s; "
            "absence-based decisions will require review",
            evaluation_id,
            len(bidder_ocr_errors),
        )
    _raise_on_ocr_errors(bidder_ocr_notes, "Bidder")
    phase_seconds["paddle_scanned_pages"] = round(time.perf_counter() - phase_started, 3)
    mark("paddle_ocr")
    phase_started = time.perf_counter()
    vendor_profile = extract_vendor_profile(approved_documents, tender_profile)
    phase_seconds["vendor_profile"] = round(time.perf_counter() - phase_started, 3)
    mark("vendor_profile")
    phase_started = time.perf_counter()
    semantic_index = bidder_semantic_index(
        [
            page
            for document in approved_documents
            for page in document.pages
            if page.text.strip()
        ]
    )
    phase_seconds["bidder_semantic_index"] = round(time.perf_counter() - phase_started, 3)
    mark("bidder_semantic_index")
    phase_started = time.perf_counter()
    evidence_scout_log = grouped_evidence_scout_agent(
        matrix, logical_documents, approved_documents, semantic_index
    )
    phase_seconds["evidence_scout"] = round(time.perf_counter() - phase_started, 3)
    evidence_map = evidence_mapper_agent(
        matrix, logical_documents, approved_documents, evidence_scout_log
    )
    evidence_packets = evidence_packet_builder_agent(
        matrix, evidence_map, logical_documents, approved_documents
    )
    evidence_ledger = evidence_ledger_builder(approved_documents, evidence_packets)
    specialist_findings = specialist_orchestrator_agent(matrix, evidence_packets)
    specialist_findings = cross_document_reconciliation_agent(
        matrix, evidence_packets, specialist_findings
    )
    mark("evidence_retrieval")
    phase_started = time.perf_counter()
    verification_set = document_verifier_agent(
        matrix,
        evidence_map,
        logical_documents,
        approved_documents,
        evidence_packets,
        specialist_findings,
        semantic_index,
    )
    phase_seconds["evidence_verification"] = round(time.perf_counter() - phase_started, 3)
    mark("evidence_verification")
    findings, criterion_validator_set = run_criteria_evaluation_stage(
        matrix,
        verification_set,
        logical_documents,
        evidence_packets,
        evidence_ledger,
        approved_documents,
        vendor_profile,
    )
    _preserve_ocr_uncertainty(findings, bidder_ocr_notes)
    from procurement.evaluation.next_ask import apply_next_ask

    try:
        next_ask_payload = apply_next_ask(
            findings,
            vendor=vendor_profile,
            tender=tender_profile,
        )
    except Exception:
        logger.exception("policy next-ask failed evaluation_id=%s", evaluation_id)
        next_ask_payload = {
            "schema_version": "bid-policy-next-ask.v1",
            "cards": [],
            "error": "failed",
        }
    mark("criterion_judging")
    report = decision_agent(
        evaluation_id,
        tender_profile,
        vendor_profile,
        approved_documents,
        logical_documents,
        findings,
        matrix,
        verification_set,
    )
    report.logical_documents = logical_documents
    report = enforce_server_validation(report, evaluation_id, approved_documents)
    from procurement.bhel_evaluation import build_bhel_extension

    bhel_extension = build_bhel_extension(matrix, findings, report)
    if bhel_extension:
        report.extensions["bhel"] = bhel_extension
    if next_ask_payload:
        report.extensions["policy_next_ask"] = next_ask_payload
    mark("report_finalization")
    handoffs = {
        "pipeline": "reviewed_bidder_evaluation.v1",
        "phase_seconds": phase_seconds,
        "04_requirement_matrix": model_dump(matrix),
        "05_vendor_logical_documents": [
            model_dump(item) for item in logical_documents
        ],
        "05a_evidence_scout_log": model_dump(evidence_scout_log),
        "06_criteria_evidence_map": model_dump(evidence_map),
        "06a_evidence_packets": model_dump(evidence_packets),
        "06b_specialist_findings": model_dump(specialist_findings),
        "06c_evidence_ledger": model_dump(evidence_ledger),
        "07_document_verification": model_dump(verification_set),
        "07a_criterion_validators": model_dump(criterion_validator_set),
        "08_criteria_findings": [model_dump(item) for item in findings],
        "08a_policy_next_ask": next_ask_payload,
        "09_internal_decision_report": model_dump(report),
        "09a_bhel_evaluation": bhel_extension,
        "11_llm_usage_and_cost": llm_usage_summary(),
        "bidder_ocr": bidder_ocr_notes,
    }
    return report, approved_documents, handoffs, len(documents) - len(approved_documents)


__all__ = [
    "EvaluationHooks",
    "approved_requirement_matrix",
    "evaluate_reviewed_bid",
    "restore_bidder_index_labels",
]
