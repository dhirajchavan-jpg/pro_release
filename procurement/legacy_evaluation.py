"""Development-only filesystem evaluation orchestration.

The PostgreSQL-backed worker owns the production pipeline. This module keeps
the legacy compatibility flow independent from the FastAPI composition root.
"""

from pathlib import Path
from typing import Any

from procurement.domain.schemas import EvaluationReport, ExtractedDocument, RequirementMatrix, TenderProfile
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text
from procurement.evaluation.assessment import decision_agent
from procurement.evaluation.criteria import normalize_prohibition_criterion
from procurement.evaluation.document_verifier import document_verifier_agent
from procurement.evaluation.evidence_pipeline import (
    cross_document_reconciliation_agent,
    evidence_ledger_builder,
    evidence_packet_builder_agent,
    specialist_orchestrator_agent,
)
from procurement.evaluation.evidence_scout import evidence_mapper_agent, evidence_scout_agent
from procurement.evaluation.ocr_recovery import run_evidence_relevant_ocr, run_targeted_ocr
from procurement.evaluation.pipeline import run_criteria_evaluation_stage
from procurement.evaluation.reporting import simple_report
from procurement.evaluation.validation import enforce_server_validation
from procurement.evaluation.vendor_profile import extract_vendor_profile
from procurement.platform.job_context import (
    JOB_CONTEXT,
    check_job_deadline,
    llm_usage_summary,
    reset_llm_usage,
)
from procurement.platform.legacy_jobs import artifact_dir_for
from procurement.understanding.amendments import tender_completeness_warnings
from procurement.understanding.base_rfp_pipeline import base_rfp_understanding_agent
from procurement.understanding.classification import classify_documents, classify_pages, reconstruct_logical_documents
from procurement.understanding.documents import load_upload_documents
from procurement.understanding.extraction import (
    requirement_matrix_from_extraction,
    tender_extraction_warnings,
    tender_profile_from_extraction,
)
from procurement.understanding.ocr_pipeline import run_base_rfp_ocr
from procurement.understanding.sources import identify_base_rfp_set, selected_base_rfp_documents
from procurement.understanding.structural_review import (
    BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED,
    run_structural_adjudication,
)
from procurement.understanding.structure import reconstruct_document_structure
from procurement.understanding.tender_projection import (
    criteria_coverage_auditor_agent,
    tender_extraction_from_base_rfp,
)


def read_guidelines_text(guidelines_paths: list[Path] | None) -> str:
    parts: list[str] = []
    for path in guidelines_paths or []:
        if path.suffix.lower() not in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return clean_text("\n".join(parts))


def apply_guideline_default_passes(tender: TenderProfile, guidelines_text: str) -> set[str]:
    return set()


def apply_guideline_default_passes_to_matrix(matrix: RequirementMatrix, criterion_ids: set[str]) -> None:
    return


def evaluate_submission(
    job_id: str,
    tender_path: Path,
    vendor_path: Path,
    work_dir: Path,
    guidelines_paths: list[Path] | None = None,
) -> tuple[EvaluationReport, list[ExtractedDocument], dict[str, Any]]:
    reset_llm_usage(job_id)
    guidelines_text = read_guidelines_text(guidelines_paths)
    tender_docs = load_upload_documents(
        tender_path,
        work_dir / "tender",
        "tender",
        input_role="tender_packet",
        document_id_prefix="tender",
    )
    for guideline_index, guidelines_path in enumerate(guidelines_paths or [], start=1):
        tender_docs.extend(
            load_upload_documents(
                guidelines_path,
                work_dir / "guidelines" / guidelines_path.stem,
                "tender",
                input_role="evaluation_guideline",
                document_id_prefix=f"guideline-{guideline_index}",
            )
        )
    vendor_docs = load_upload_documents(
        vendor_path,
        work_dir / "vendor",
        "vendor",
        input_role="vendor_submission",
        document_id_prefix="vendor",
    )
    docs = tender_docs + vendor_docs

    check_job_deadline()
    checkpoint_root = artifact_dir_for(job_id) / "checkpoints"
    base_rfp_ocr_notes = run_base_rfp_ocr(tender_docs, checkpoint_root / "ocr")
    classify_documents(docs)
    classify_pages(docs)
    logical_documents = reconstruct_logical_documents(docs)
    tender_structure = reconstruct_document_structure(
        tender_docs,
        [item for item in logical_documents if item.upload_side == "tender"],
    )
    if BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED:
        tender_structure = run_structural_adjudication(
            tender_docs,
            [item for item in logical_documents if item.upload_side == "tender"],
            tender_structure,
            checkpoint_root / "structure",
        )
    base_rfp_selection = identify_base_rfp_set(
        logical_documents,
        [page for document in tender_docs for page in document.pages],
    )
    base_rfp_understanding = base_rfp_understanding_agent(
        tender_docs,
        logical_documents,
        base_rfp_selection,
        checkpoint_root / "semantic",
    )
    base_rfp_docs = selected_base_rfp_documents(base_rfp_selection, tender_docs)
    check_job_deadline()
    tender_extraction = tender_extraction_from_base_rfp(base_rfp_understanding, base_rfp_docs)
    tender_profile = tender_profile_from_extraction(tender_extraction)
    criteria_coverage_auditor_agent(base_rfp_docs, tender_profile)
    default_pass_criteria = apply_guideline_default_passes(tender_profile, guidelines_text)
    tender_warnings = tender_completeness_warnings(base_rfp_docs, tender_profile)
    tender_warnings.extend(tender_extraction_warnings(tender_extraction))
    if any(item.get("role") == "post_base_update" for item in base_rfp_selection):
        tender_warnings.append(
            "Corrigenda or pre-bid clarifications were isolated from the base RFP and await the versioning layer."
        )
    requirement_matrix = requirement_matrix_from_extraction(tender_extraction, tender_profile)
    for item in requirement_matrix.items:
        normalize_prohibition_criterion(item)
    apply_guideline_default_passes_to_matrix(requirement_matrix, default_pass_criteria)
    check_job_deadline()
    vendor_profile = extract_vendor_profile(vendor_docs, tender_profile)
    evidence_scout_log = evidence_scout_agent(requirement_matrix, logical_documents, docs)
    evidence_map = evidence_mapper_agent(requirement_matrix, logical_documents, docs, evidence_scout_log)
    evidence_packets = evidence_packet_builder_agent(requirement_matrix, evidence_map, logical_documents, docs)
    evidence_ledger = evidence_ledger_builder(docs, evidence_packets)
    evidence_relevant_ocr_notes = run_evidence_relevant_ocr(
        vendor_docs,
        requirement_matrix,
        evidence_map,
        logical_documents,
        evidence_packets,
    )
    if any(note.get("status") == "ok" for note in evidence_relevant_ocr_notes):
        check_job_deadline()
        logical_documents = reconstruct_logical_documents(docs)
        vendor_profile = extract_vendor_profile(vendor_docs, tender_profile)
        evidence_scout_log = evidence_scout_agent(requirement_matrix, logical_documents, docs)
        evidence_map = evidence_mapper_agent(requirement_matrix, logical_documents, docs, evidence_scout_log)
        evidence_packets = evidence_packet_builder_agent(requirement_matrix, evidence_map, logical_documents, docs)
        evidence_ledger = evidence_ledger_builder(docs, evidence_packets)
    specialist_findings = specialist_orchestrator_agent(requirement_matrix, evidence_packets)
    specialist_findings = cross_document_reconciliation_agent(
        requirement_matrix,
        evidence_packets,
        specialist_findings,
    )
    verification_set = document_verifier_agent(
        requirement_matrix,
        evidence_map,
        logical_documents,
        docs,
        evidence_packets,
        specialist_findings,
    )
    findings, criterion_validator_set = run_criteria_evaluation_stage(
        requirement_matrix,
        verification_set,
        logical_documents,
        evidence_packets,
        evidence_ledger,
        docs,
        vendor_profile,
    )
    targeted_ocr_notes = run_targeted_ocr(
        vendor_docs,
        requirement_matrix,
        evidence_map,
        findings,
        logical_documents,
    )
    if any(note.get("status") == "ok" for note in targeted_ocr_notes):
        check_job_deadline()
        logical_documents = reconstruct_logical_documents(docs)
        vendor_profile = extract_vendor_profile(vendor_docs, tender_profile)
        evidence_scout_log = evidence_scout_agent(requirement_matrix, logical_documents, docs)
        evidence_map = evidence_mapper_agent(requirement_matrix, logical_documents, docs, evidence_scout_log)
        evidence_packets = evidence_packet_builder_agent(requirement_matrix, evidence_map, logical_documents, docs)
        evidence_ledger = evidence_ledger_builder(docs, evidence_packets)
        specialist_findings = specialist_orchestrator_agent(requirement_matrix, evidence_packets)
        specialist_findings = cross_document_reconciliation_agent(
            requirement_matrix,
            evidence_packets,
            specialist_findings,
        )
        verification_set = document_verifier_agent(
            requirement_matrix,
            evidence_map,
            logical_documents,
            docs,
            evidence_packets,
            specialist_findings,
        )
        findings, criterion_validator_set = run_criteria_evaluation_stage(
            requirement_matrix,
            verification_set,
            logical_documents,
            evidence_packets,
            evidence_ledger,
            docs,
            vendor_profile,
        )
    report = decision_agent(job_id, tender_profile, vendor_profile, docs, logical_documents, findings)
    report.logical_documents = logical_documents
    report.manual_review_reasons.extend(tender_warnings)
    report = enforce_server_validation(report, job_id, docs)
    handoffs = {
        "01_file_inventory": [model_dump(doc.record) for doc in docs],
        "02_extracted_units": [model_dump(page) for doc in docs for page in doc.pages],
        "02_base_rfp_layout_ocr": base_rfp_ocr_notes,
        "02_base_rfp_structure": model_dump(tender_structure),
        "02_base_rfp_selection": base_rfp_selection,
        "03_base_rfp_atoms": list(getattr(JOB_CONTEXT, "base_rfp_atoms", []) or []),
        "03_base_rfp_schema": model_dump(base_rfp_understanding),
        "02a_evidence_relevant_ocr": evidence_relevant_ocr_notes,
        "02b_targeted_ocr": targeted_ocr_notes,
        "03_tender_extraction": model_dump(tender_extraction),
        "03_rfp_understanding": model_dump(tender_profile),
        "03a_tender_packet_warnings": tender_warnings,
        "04_requirement_matrix": model_dump(requirement_matrix),
        "05_vendor_logical_documents": [model_dump(item) for item in logical_documents if item.upload_side == "vendor"],
        "05a_evidence_scout_log": model_dump(evidence_scout_log),
        "06_criteria_evidence_map": model_dump(evidence_map),
        "06a_evidence_packets": model_dump(evidence_packets),
        "06b_specialist_findings": model_dump(specialist_findings),
        "06c_evidence_ledger": model_dump(evidence_ledger),
        "07_document_verification": model_dump(verification_set),
        "07a_criterion_validators": model_dump(criterion_validator_set),
        "08_criteria_findings": [model_dump(item) for item in findings],
        "09_internal_decision_report": model_dump(report),
        "10_final_public_report": simple_report(report),
        "11_llm_usage_and_cost": llm_usage_summary(),
    }
    return report, docs, handoffs
