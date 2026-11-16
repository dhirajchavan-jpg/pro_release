"""Criterion evaluation orchestration independent from API assembly."""

from procurement.domain.catalog import PROCUREMENT_SOURCES
from procurement.domain.schemas import (
    CriteriaFinding,
    CriterionValidatorSet,
    DocumentVerificationSet,
    EvaluationReport,
    EvidenceLedger,
    EvidencePacketSet,
    ExtractedDocument,
    LogicalDocument,
    RequirementMatrix,
    TenderProfile,
    VendorProfile,
)
from procurement.domain.serialization import document_context, model_dump, validate_model
from procurement.evaluation.assessment import (
    apply_positive_rollup_statuses,
    apply_rollup_and_downstream_statuses,
    criteria_evaluator_agent,
    deterministic_criterion_validator_agent,
    enforce_required_evidence_gates,
)
from procurement.evaluation.criterion_judge import gemini_criterion_judge_agent
from procurement.evaluation.normalization import normalize_evaluation_data
from procurement.evaluation.validators import apply_criterion_validator_results
from procurement.platform.llm import GEMINI_MODEL_DEEP, llm_json
from procurement.platform.prompts import prompt_text


def run_criteria_evaluation_stage(
    matrix: RequirementMatrix,
    verification_set: DocumentVerificationSet,
    logical_documents: list[LogicalDocument],
    evidence_packets: EvidencePacketSet,
    evidence_ledger: EvidenceLedger,
    docs: list[ExtractedDocument],
    vendor_profile: VendorProfile | None = None,
) -> tuple[list[CriteriaFinding], CriterionValidatorSet]:
    findings = criteria_evaluator_agent(
        matrix,
        verification_set,
        logical_documents,
        apply_rollups=False,
        docs=docs,
    )
    validator_set = deterministic_criterion_validator_agent(
        matrix,
        findings,
        verification_set,
        evidence_packets,
        evidence_ledger,
        docs,
    )
    apply_criterion_validator_results(findings, validator_set, matrix.items)
    findings = gemini_criterion_judge_agent(
        matrix,
        verification_set,
        evidence_packets,
        findings,
        logical_documents,
        docs,
        model_dump(vendor_profile) if vendor_profile is not None else None,
    )
    enforce_required_evidence_gates(findings, matrix, verification_set)
    apply_criterion_validator_results(findings, validator_set, matrix.items)
    apply_positive_rollup_statuses(findings, matrix.items)
    apply_rollup_and_downstream_statuses(findings, matrix.items)
    return findings, validator_set


def evaluate_against_tender(
    job_id: str,
    tender: TenderProfile,
    vendor: VendorProfile,
    docs: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
) -> EvaluationReport:
    data = llm_json(
        prompt_text("final_evaluator", "You are a strict Indian government procurement evaluator. Return only JSON."),
        {
            "task": "evaluate_bid",
            "procurement_sources": PROCUREMENT_SOURCES,
            "rules": [
                "Evaluate only against criteria found in the tender/RFP.",
                "Return exactly one finding for every tender_profile.criteria item; do not omit criteria and do not invent new criteria.",
                "A missing mandatory document or unmet mandatory criterion must be marked missing or fail.",
                "Do not pass a criterion without document evidence.",
                "Financial opening/ranking is only meaningful if mandatory and technical criteria pass.",
                "Do not auto-cure missing documents, auto-negotiate, or apply preference to a technically non-responsive bid.",
                "Prefer fail or unclear over pass when evidence is weak.",
            ],
            "job_id": job_id,
            "tender_profile": model_dump(tender),
            "vendor_profile": model_dump(vendor),
            "classified_documents": [model_dump(doc.record) for doc in docs],
            "logical_documents": [model_dump(doc) for doc in logical_documents],
            "document_text": document_context([doc for doc in docs if doc.record.upload_side == "vendor"]),
            "json_shape": {
                "job_id": job_id,
                "procurement_source": "gem|cppp|department_portal|manual|unknown",
                "procurement_category": "goods|works|services|consultancy|composite|unknown",
                "evaluation_method": "single_packet_l1|two_packet_l1|two_stage|qcbs|reverse_auction|unknown",
                "qualifies": False,
                "recommendation": "technically_qualified|rejected|needs_manual_review",
                "summary": "short evaluation summary",
                "tender": "same tender_profile object",
                "vendor": "same vendor_profile object",
                "documents": "classified_documents array",
                "logical_documents": "detected/reconstructed logical documents array",
                "findings": [
                    {
                        "criterion_id": "C1",
                        "criterion_text": "criterion requirement",
                        "category": "eligibility/technical/etc",
                        "mandatory": True,
                        "status": "pass|fail|missing|unclear",
                        "reason": "why",
                        "required_documents": ["docs required"],
                        "matched_documents": ["document ids used"],
                        "evidence": [
                            {
                                "document_id": "vendor-1",
                                "file_name": "file.pdf",
                                "quote": "short quote",
                                "confidence": 0.8,
                            }
                        ],
                    }
                ],
                "responsiveness": {
                    "status": "responsive|unresponsive|clarification_required",
                    "substantive_deviations": ["material deviations"],
                    "minor_deviations": ["clarifiable shortfalls"],
                    "strict_fail_reasons": ["mandatory fail reasons"],
                },
                "eligibility_result": {
                    "status": "pass|fail|not_applicable|unclear",
                    "reason": "summary",
                    "failed_criteria": ["failed eligibility criteria"],
                },
                "technical_result": {
                    "status": "pass|fail|score_based|unclear",
                    "reason": "summary",
                    "failed_criteria": ["failed technical criteria"],
                    "score": None,
                    "minimum_score": None,
                },
                "commercial_result": {
                    "status": "pass|fail|unclear",
                    "reason": "summary",
                    "failed_criteria": ["commercial deviations"],
                },
                "financial_result": {
                    "status": "not_opened|pass|fail|unclear",
                    "opening_allowed": False,
                    "evaluated_price": None,
                    "rank": None,
                    "price_components": {},
                },
                "missing_mandatory": ["missing mandatory document or requirement"],
                "manual_review_reasons": ["unclear but non-rejection-grade issues"],
                "risk_flags": ["issues requiring human review"],
                "financial_summary": {"quoted_price": "if found"},
                "final_decision": "reject_unresponsive|reject_technical|financial_opening_allowed|l1_recommended|award_recommended|needs_manual_review",
                "audit_log": [
                    {
                        "rule_source": "GFR/DoE/GeM/tender clause",
                        "finding": "short audit finding",
                        "document_id": "optional document id",
                        "quote": "optional short quote",
                    }
                ],
                "validation": {},
            },
        },
        model=GEMINI_MODEL_DEEP,
    )
    normalize_evaluation_data(data)
    return validate_model(EvaluationReport, data)
