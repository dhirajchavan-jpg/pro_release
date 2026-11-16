"""Stable public evaluation-result presentation rules."""

from __future__ import annotations

import re
from typing import Any, Literal

from procurement.domain.schemas import (
    CriteriaFinding,
    Criterion,
    EvaluationReport,
    PublicCriterionResult,
    PublicEvaluationResult,
    PublicFinancialOpening,
    PublicManualReview,
    PublicMissingDocument,
    PublicTenderCriterion,
    PublicTenderSummary,
    PublicValidationSummary,
    PublicVendorSummary,
    TenderProfile,
)
from procurement.domain.serialization import model_dump, validate_model
from procurement.domain.text import normalize_for_match, unique_items
from procurement.evaluation.normalization import is_post_award_criterion
from procurement.evaluation.validation import normalize_status_reason


def simple_report(report: EvaluationReport) -> dict[str, Any]:
    return model_dump(public_report_model(report))


def public_report_model(report: EvaluationReport) -> PublicEvaluationResult:
    doc_names = {document.document_id: document.file_name for document in report.documents}
    # The canonical ledger retains every frozen item.  The established public
    # bid-results projection shows only bid-stage checks and identifies scope
    # from the canonical fields rather than a wording heuristic.
    bid_stage_criteria = [
        criterion
        for criterion in report.tender.criteria
        if criterion.criterion_kind not in {"obligation", "post_award"}
        and criterion.evaluation_scope not in {"post_award", "commercial_out_of_scope"}
        and not is_post_award_criterion(criterion)
    ]
    criteria_by_id = {criterion.criterion_id: (index, criterion) for index, criterion in enumerate(bid_stage_criteria, start=1)}
    public_findings = [finding for finding in report.findings if finding.criterion_id in criteria_by_id]
    criteria = [
        PublicTenderCriterion(
            no=index,
            category=criterion.category,
            requirement=sanitize_public_text(criterion.requirement),
            mandatory=criterion.mandatory,
            pass_condition=sanitize_public_text(criterion.pass_condition),
            required_documents=sanitize_public_list(criterion.required_documents),
        )
        for index, criterion in enumerate(bid_stage_criteria, start=1)
    ]
    criterion_results = [
        public_criterion_result(finding, criteria_by_id, doc_names)
        for finding in public_findings
    ]
    missing_documents = public_missing_documents(public_findings, criteria_by_id)
    missing_documents = suppress_downstream_missing_documents(public_findings, missing_documents)
    manual_reasons = public_manual_review_reasons(report)
    public = PublicEvaluationResult(
        bid_id=report.job_id,
        final_decision=report.final_decision,
        recommendation=report.recommendation,
        qualifies=report.qualifies,
        summary=public_summary(report, missing_documents, manual_reasons),
        next_action=next_action(report, missing_documents, manual_reasons),
        tender=PublicTenderSummary(
            title=report.tender.tender_title,
            tender_number=sanitize_public_text(report.tender.tender_number),
            buyer=report.tender.buyer,
            procurement_type=report.tender.procurement_type,
            bid_system=report.tender.bid_system,
            scope=report.tender.scope,
            summary=tender_summary(report.tender),
            mandatory_documents=sanitize_public_list(report.tender.mandatory_documents),
            criteria=criteria,
        ),
        vendor=PublicVendorSummary(
            name=report.vendor.vendor_name,
            identifiers=report.vendor.identifiers,
            submitted_documents=public_submitted_documents(report),
            missing_documents=sanitize_public_list([item.document_name for item in missing_documents]),
        ),
        criteria_results=criterion_results,
        missing_documents=missing_documents,
        manual_review=PublicManualReview(required=bool(manual_reasons), reasons=manual_reasons),
        financial_opening=PublicFinancialOpening(
            allowed=bool(report.financial_result.opening_allowed),
            reason=financial_opening_reason(report, missing_documents, manual_reasons),
        ),
        validation=PublicValidationSummary(
            schema_valid=bool(report.validation.get("schema_valid", True)),
            strict_mandatory_fail_applied=bool(report.validation.get("strict_mandatory_fail_applied", True)),
            internal_audit_available=True,
        ),
    )
    return validate_model(PublicEvaluationResult, model_dump(public))


def public_submitted_documents(report: EvaluationReport) -> list[str]:
    logical_titles = [
        document.title
        for document in report.logical_documents
        if document.upload_side == "vendor" and document.title.strip().lower() not in {"", "unknown"}
    ]
    if logical_titles:
        return sanitize_public_list(unique_items(logical_titles))
    submitted = sanitize_public_list(unique_items(report.vendor.documents_submitted))
    if submitted:
        return submitted
    return sanitize_public_list(unique_items([document.file_name for document in report.documents if document.upload_side == "vendor"]))


def public_criterion_result(
    finding: CriteriaFinding,
    criteria_by_id: dict[str, tuple[int, Criterion]],
    doc_names: dict[str, str],
) -> PublicCriterionResult:
    index, criterion = criteria_by_id.get(
        finding.criterion_id,
        (0, Criterion(criterion_id="", requirement=finding.criterion_text, category=finding.category)),
    )
    missing_documents = inferred_missing_documents(finding)
    status = public_status_for_finding(finding, missing_documents)
    if finding_depends_on_incomplete_tender_detail(finding, criteria_by_id) and status in {"missing", "failed"}:
        status = "needs_manual_review"
        missing_documents = []
    evidence_documents = sorted(
        {
            doc_names[evidence.document_id]
            for evidence in finding.evidence
            if evidence.document_id in doc_names
        }
    )
    return PublicCriterionResult(
        no=index,
        category=criterion.category or finding.category,
        requirement=sanitize_public_text(criterion.requirement or finding.criterion_text),
        status=status,
        result={
            "passed": "Passed",
            "failed": "Failed",
            "missing": "Missing required document",
            "needs_manual_review": "Needs manual review",
            "not_applicable": "Not applicable",
            "not_evaluated": "Processing incomplete",
        }[status],
        why=sanitize_public_text(normalize_status_reason(status, finding.reason)),
        required_documents=sanitize_public_list(finding.required_documents or criterion.required_documents),
        missing_documents=sanitize_public_list(missing_documents),
        evidence_documents=sanitize_public_list(evidence_documents),
    )


def public_missing_documents(
    findings: list[CriteriaFinding],
    criteria_by_id: dict[str, tuple[int, Criterion]] | None = None,
) -> list[PublicMissingDocument]:
    missing: list[PublicMissingDocument] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        if not finding.mandatory:
            continue
        if finding.status == "fail" and not failed_finding_is_incomplete_required_document(finding):
            continue
        if finding.status not in {"missing", "fail"}:
            continue
        if criteria_by_id and finding_depends_on_incomplete_tender_detail(finding, criteria_by_id):
            continue
        docs = inferred_missing_documents(finding)
        if not docs:
            continue
        for document_name in docs:
            key = (document_name, finding.criterion_text)
            if key in seen:
                continue
            seen.add(key)
            missing.append(
                PublicMissingDocument(
                    document_name=sanitize_public_text(document_name),
                    required_for=sanitize_public_text(finding.criterion_text),
                    impact="Mandatory document missing; bid cannot proceed to financial opening.",
                )
            )
    return missing


def finding_depends_on_incomplete_tender_detail(
    finding: CriteriaFinding,
    criteria_by_id: dict[str, tuple[int, Criterion]],
) -> bool:
    criterion_entry = criteria_by_id.get(finding.criterion_id)
    if not criterion_entry:
        return False
    return bool(criterion_entry[1].clarifiable)


def suppress_downstream_missing_documents(
    findings: list[CriteriaFinding],
    missing_documents: list[PublicMissingDocument],
) -> list[PublicMissingDocument]:
    blocking_rank = earliest_blocking_stage_rank(findings)
    if blocking_rank is None:
        return missing_documents
    allowed_requirements = {
        sanitize_public_text(finding.criterion_text)
        for finding in findings
        if stage_rank(finding.category) <= blocking_rank
    }
    findings_by_requirement = {
        sanitize_public_text(finding.criterion_text): finding
        for finding in findings
    }
    kept: list[PublicMissingDocument] = []
    for item in missing_documents:
        if item.required_for in allowed_requirements:
            kept.append(item)
            continue
        finding = findings_by_requirement.get(item.required_for)
        if finding and downstream_missing_artifact(finding):
            continue
        kept.append(item)
    return kept


def downstream_missing_artifact(finding: CriteriaFinding) -> bool:
    text = normalize_for_match(f"{finding.reason} {finding.criterion_text}")
    return any(
        phrase in text
        for phrase in [
            "not evaluated after",
            "after prior rejection",
            "after prior failure",
            "downstream",
            "do not infer document absence",
        ]
    )


def earliest_blocking_stage_rank(findings: list[CriteriaFinding]) -> int | None:
    ranks = [
        stage_rank(finding.category)
        for finding in findings
        if finding.mandatory and finding.status in {"fail", "missing"}
    ]
    return min(ranks) if ranks else None


def stage_rank(category: str) -> int:
    text = normalize_for_match(category)
    if any(term in text for term in ["emd", "mse", "msme", "eligibility", "bqc", "pqc", "qualification"]):
        return 10
    if "technical" in text or "experience" in text or "work" in text:
        return 20
    if "commercial" in text or "declaration" in text or "compliance" in text:
        return 30
    if "financial" in text or "price" in text:
        return 40
    return 30


def tender_packet_incomplete_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if "Tender packet may be incomplete" in reason]


def public_status_for_finding(finding: CriteriaFinding, missing_documents: list[str]) -> Literal["passed", "failed", "missing", "needs_manual_review", "not_applicable", "not_evaluated"]:
    return {
        "pass": "passed",
        "fail": "failed",
        "missing": "missing",
        "unclear": "needs_manual_review",
        "not_applicable": "not_applicable",
        "not_evaluated": "not_evaluated",
    }[finding.status]


def inferred_missing_documents(finding: CriteriaFinding) -> list[str]:
    if finding.status == "fail" and not failed_finding_is_incomplete_required_document(finding):
        return []
    if finding.status not in {"missing", "fail"}:
        return []
    return finding.missing_documents or finding.required_documents


def failed_finding_is_incomplete_required_document(finding: CriteriaFinding) -> bool:
    text = normalize_for_match(f"{finding.reason} {' '.join(finding.required_documents)}")
    return any(
        phrase in text
        for phrase in [
            "financial years",
            "complete three year",
            "not submitted",
            "not provided",
            "not found",
            "missing or unverified required check",
            "presence",
        ]
    )


def public_manual_review_reasons(report: EvaluationReport) -> list[str]:
    has_hard_mandatory_failure = any(
        finding.mandatory and finding.status in {"fail", "missing"}
        for finding in report.findings
    )
    if has_hard_mandatory_failure and report.recommendation != "needs_manual_review" and report.final_decision != "needs_manual_review":
        return []
    reasons = [
        sanitize_public_text(finding.reason)
        for finding in report.findings
        if finding.status == "unclear"
    ]
    reasons.extend(sanitize_public_text(reason) for reason in report.manual_review_reasons)
    return sorted({reason for reason in reasons if reason})


def public_summary(
    report: EvaluationReport,
    missing_documents: list[PublicMissingDocument],
    manual_reasons: list[str],
) -> str:
    if report.final_decision == "needs_manual_review":
        return "Bid needs manual review before financial opening."
    if missing_documents:
        names = ", ".join(item.document_name for item in missing_documents[:3])
        return f"Bid is rejected because mandatory document(s) are missing: {names}."
    if report.recommendation == "rejected":
        failed_reasons = [sanitize_public_text(finding.reason) for finding in report.findings if finding.status == "fail"]
        if failed_reasons:
            return f"Bid is rejected because: {failed_reasons[0]}"
        return "Bid is rejected because one or more mandatory criteria failed."
    if report.recommendation == "needs_manual_review" or manual_reasons:
        return "Bid needs manual review before financial opening."
    if report.qualifies:
        return "Bid is technically qualified. Financial opening is allowed."
    return sanitize_public_text(report.summary)


def next_action(
    report: EvaluationReport,
    missing_documents: list[PublicMissingDocument],
    manual_reasons: list[str],
) -> str:
    if report.final_decision == "needs_manual_review":
        return "Review the flagged evidence manually before opening the financial bid."
    if missing_documents:
        return "Reject as non-responsive unless the tender explicitly allows clarification for these missing documents."
    if report.recommendation == "rejected":
        return "Reject as technically non-compliant; do not open the financial bid."
    if report.recommendation == "needs_manual_review" or manual_reasons:
        return "Review the flagged evidence manually before opening the financial bid."
    if report.qualifies:
        return "Proceed to financial opening."
    return "Review the evaluation detail before taking procurement action."


def financial_opening_reason(
    report: EvaluationReport,
    missing_documents: list[PublicMissingDocument],
    manual_reasons: list[str],
) -> str:
    if report.financial_result.opening_allowed:
        return "All mandatory checks passed."
    if missing_documents:
        return "Blocked because mandatory document(s) are missing."
    if report.recommendation == "rejected":
        return "Blocked because one or more mandatory criteria failed."
    if manual_reasons:
        return "Blocked until manual review is completed."
    return "Blocked by responsiveness validation."


def tender_summary(tender: TenderProfile) -> str:
    bits = [tender.scope, tender.procurement_type, tender.bid_system]
    text = " | ".join(bit for bit in bits if bit and bit.lower() != "unknown")
    return text or tender.tender_title


def sanitize_public_text(value: str | None) -> str:
    text = str(value or "")
    text = re.sub(r"\s*\([^)]*\bLD-\d+[^)]*\)", "", text)
    text = re.sub(r"\bLD-\d+\b", "submitted document", text)
    text = re.sub(r"\bC\d+\s*:\s*", "", text)
    text = re.sub(r"\bmandatory criterion unclear\s*-\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sanitize_public_list(values: list[str]) -> list[str]:
    return [text for text in (sanitize_public_text(value) for value in values) if text]
