"""Server-side evaluation validation and evidence grounding gates."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.catalog import PROCUREMENT_SOURCES
from procurement.domain.schemas import (
    CriteriaFinding,
    DocumentVerification,
    EvaluationReport,
    EvidenceMapEntry,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
)
from procurement.domain.text import (
    clean_text,
    keyword_tokens,
    normalize_for_match,
    quote_matches_text,
    unique_items,
)
from procurement.evaluation.facts import exact_product_identity_anchors
from procurement.evaluation.criteria import criterion_response_mode
from procurement.evaluation.normalization import is_post_award_criterion
from procurement.understanding.classification import tender_source_trap


def exact_product_identity_proven(requirement: str, evidence_text: str) -> bool:
    anchors = exact_product_identity_anchors(requirement)
    if len(anchors) < 2:
        return False
    submitted = normalize_for_match(evidence_text)
    return all(anchor in submitted for anchor in anchors)


def evidence_proof_contract_gap(
    evidence: EvidenceRequirement,
    entry: EvidenceMapEntry,
    logical_by_id: dict[str, LogicalDocument],
    verification: DocumentVerification | None = None,
    *,
    selected_page_texts: list[str] | None = None,
    criterion: Any | None = None,
) -> str | None:
    """Return a generic proof-strength gap without upgrading weak evidence."""
    contract = dict(evidence.proof_contract or {})
    if not contract:
        return None
    matched = [
        logical_by_id[logical_id]
        for logical_id in entry.matched_logical_document_ids
        if logical_id in logical_by_id
    ]
    labels = {
        str(label).strip().casefold()
        for document in matched
        for label in ([document.document_type] + list(document.labels or []))
        if str(label).strip()
    }
    capabilities = {
        str(capability).strip().casefold()
        for document in matched
        for capability in document.capabilities or []
        if str(capability).strip()
    }
    descriptive_text = " ".join(
        f"{document.title} {document.document_type} {document.content_summary}"
        for document in matched
    ).casefold()
    verified_strengths: set[str] = set()
    verified_text = ""
    if verification is not None:
        passing_checks = {
            str(check).strip().casefold()
            for check, status in verification.check_results.items()
            if str(status).strip().casefold() == "pass"
        }
        if passing_checks.intersection({"signed", "signature", "signature_present"}):
            capabilities.add("signature_evidence")
        if passing_checks.intersection({"stamped", "stamp", "seal", "seal_present"}):
            capabilities.add("seal_evidence")
        if passing_checks.intersection({"udin", "udin_if_ca"}):
            capabilities.add("financial_capacity")
        if passing_checks.intersection({"mse_udyam", "udyam", "registration"}):
            capabilities.update({"mse_status", "statutory_identifier"})
            verified_strengths.add("issuer_certificate")
        if {"legal_entity_identity", "participation_structure"}.issubset(passing_checks):
            capabilities.add("bidder_identity")
        specialist_capabilities = {
            "udyam_mse": ("issuer_certificate", {"mse_status", "statutory_identifier"}),
            "financial_udin": ("issuer_certificate", {"financial_capacity"}),
            "po_work_order": ("issuer_certificate", {"experience_award"}),
            "completion_execution": (
                "issuer_certificate",
                {"experience_completion", "execution_proof"},
            ),
            "local_content": ("issuer_certificate", {"local_content_proof"}),
            "declaration": ("bidder_declaration", {"bidder_declaration"}),
            "technical_compliance": (
                "bidder_declaration",
                {"technical_compliance"},
            ),
            "third_party_assurance": (
                "third_party_verified",
                {"third_party_verification"},
            ),
        }
        for raw in verification.specialist_findings:
            if (
                not isinstance(raw, dict)
                or str(raw.get("status") or "").casefold() != "pass"
            ):
                continue
            specialist = str(raw.get("specialist") or "").casefold()
            strength, additions = specialist_capabilities.get(
                specialist,
                ("claim", set()),
            )
            verified_strengths.add(strength)
            capabilities.update(additions)
        verified_text = normalize_for_match(
            " ".join(
                [
                    verification.quote or "",
                    verification.reason or "",
                    *(selected_page_texts or []),
                ]
            )
        )
        if verification.status == "pass" and exact_product_identity_proven(
            evidence.document_name,
            verified_text,
        ):
            return None
        if verification.status == "pass" and any(
            marker in verified_text
            for marker in (
                "third party inspection",
                "tpia",
                "bureau veritas",
                "independent inspection",
            )
        ):
            capabilities.add("third_party_verification")
            verified_strengths.add("third_party_verified")
            facility_scope = any(
                marker in verified_text
                for marker in (
                    "facility verification",
                    "manufacturing facility",
                    "inspection place",
                    "plant inspection",
                    "visit to premises",
                )
            ) or ("field inspection" in verified_text and "manufacturer" in verified_text)
            if facility_scope:
                capabilities.add("facility_verification")
    if labels.intersection({"tpia_report", "third_party_inspection"}) or any(
        marker in descriptive_text
        for marker in (
            "third party inspection",
            "tpia",
            "bureau veritas",
            "independent inspection",
        )
    ):
        observed_strength = "third_party_verified"
    elif any(
        marker in label
        for label in labels
        for marker in (
            "certificate",
            "registration",
            "licence",
            "license",
            "purchase_order",
            "completion",
            "invoice",
        )
    ):
        observed_strength = "issuer_certificate"
    elif "declaration" in labels or "undertaking" in descriptive_text:
        observed_strength = "bidder_declaration"
    else:
        observed_strength = "claim"
    rank = {
        "claim": 0,
        "bidder_declaration": 1,
        "issuer_certificate": 2,
        "third_party_verified": 3,
    }
    if verified_strengths:
        observed_strength = max(
            verified_strengths | {observed_strength},
            key=lambda value: rank.get(value, 0),
        )
    required_strength = str(contract.get("minimum_strength") or "claim").casefold()
    if (
        required_strength == "bidder_declaration"
        and verification is not None
        and criterion is not None
        and structured_bidder_response_is_declaration(
            criterion,
            verification,
            selected_page_texts or [],
        )
    ):
        observed_strength = "bidder_declaration"
    if rank.get(observed_strength, 0) < rank.get(required_strength, 0):
        return (
            f"The shortlisted material is only {observed_strength.replace('_', ' ')}; "
            f"this criterion requires {required_strength.replace('_', ' ')} proof."
        )
    expected_labels = {
        str(label).strip().casefold()
        for label in contract.get("accepted_artifact_labels") or []
        if str(label).strip().casefold() not in {"", "unknown"}
    }
    required_capabilities = {
        str(value).strip().casefold()
        for value in contract.get("required_capabilities") or []
        if str(value).strip()
    }
    capability_groups = [
        {str(value).strip().casefold() for value in group if str(value).strip()}
        for group in contract.get("required_capability_groups") or []
        if isinstance(group, (list, tuple, set))
    ]
    if (
        expected_labels
        and labels
        and not labels.intersection(expected_labels)
        and not required_capabilities
        and not capability_groups
    ):
        return "The submitted artifact does not match the proof type required by this criterion."
    missing_capabilities = sorted(required_capabilities - capabilities)
    if missing_capabilities:
        return (
            "The submitted artifact does not prove the required capability: "
            + ", ".join(missing_capabilities)
            + "."
        )
    missing_groups = [
        group for group in capability_groups if group and not group.intersection(capabilities)
    ]
    if missing_groups:
        return (
            "The submitted artifact does not prove any permitted alternative capability: "
            + "; ".join(" or ".join(sorted(group)) for group in missing_groups)
            + "."
        )
    return None


def structured_bidder_response_is_declaration(
    criterion: Any,
    verification: DocumentVerification,
    selected_page_texts: list[str],
) -> bool:
    """Treat an attributable filled compliance row as the bidder's declaration."""
    mode = criterion_response_mode(criterion)
    if mode not in {"confirmation", "value"} or verification.status != "pass":
        return False
    quote = normalize_for_match(verification.quote or "")
    if not quote or tender_source_trap(quote):
        return False
    negative = re.search(r"\b(?:not confirmed|not accepted|not complied|no deviation not accepted)\b", quote)
    if negative:
        return False
    if mode == "confirmation":
        return bool(re.search(r"\b(?:confirm(?:ed)?|accept(?:ed)?|agree(?:d)?|complied|yes)\b", quote))

    source_text = normalize_for_match(
        f"{getattr(criterion, 'requirement', '')} {getattr(criterion, 'source_quote', '') or ''}"
    )
    generic = {
        "bidder", "specify", "specified", "state", "stated", "mention", "mentioned",
        "confirm", "confirmed", "accept", "accepted", "comply", "complied", "yes",
        "make", "model", "details", "requirement", "technical", "offer", "latest",
        "version", "suitable", "system", "provided", "shall", "must",
    }
    answer_tokens = keyword_tokens(quote) - keyword_tokens(source_text) - generic
    return bool(answer_tokens or re.search(r"\b\d+[a-z0-9./-]*\b", quote))


def grounded_verification_page_ids(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    page_text_by_id: dict[str, str],
) -> list[str]:
    selected = unique_items(
        [page_id for page_id in verification.page_ids if page_id in entry.matched_page_ids]
    )
    quote = normalize_for_match(verification.quote or "")
    if quote:
        exact = [
            page_id
            for page_id in (selected or entry.matched_page_ids)
            if quote in normalize_for_match(page_text_by_id.get(page_id, ""))
        ]
        if exact:
            return exact
        quote_prefix = quote[:120].strip()
        if quote_prefix:
            prefix_matches = [
                page_id
                for page_id in (selected or entry.matched_page_ids)
                if quote_prefix in normalize_for_match(page_text_by_id.get(page_id, ""))
            ]
            if prefix_matches:
                return prefix_matches
        quote_tokens = set(re.findall(r"[a-z0-9]+", quote))
        if len(quote_tokens) >= 6:
            overlap_matches = []
            for page_id in selected or entry.matched_page_ids:
                page_tokens = set(re.findall(r"[a-z0-9]+", normalize_for_match(page_text_by_id.get(page_id, ""))))
                overlap = len(quote_tokens & page_tokens) / len(quote_tokens)
                if overlap >= 0.75:
                    overlap_matches.append((overlap, page_id))
            if overlap_matches:
                overlap_matches.sort(reverse=True)
                return [page_id for _score, page_id in overlap_matches[:3]]
    visually_inspected = {
        str(check.get("page_id") or "")
        for check in verification.visual_checks
        if check.get("status") == "inspected" and check.get("page_id")
    }
    return [page_id for page_id in selected if page_id in visually_inspected]


def objective_verification_reason(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
) -> str:
    source = ", ".join(verification.page_ids)
    quote = clean_text(verification.quote or "")
    identifiers: list[str] = []
    patterns = [
        ("GSTIN", r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b"),
        ("PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b"),
        ("Udyam", r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b"),
        ("ISO", r"\bISO\s*\d{4,5}(?::\d{4})?\b"),
    ]
    upper_quote = quote.upper()
    for label, pattern in patterns:
        match = re.search(pattern, upper_quote)
        if match:
            identifiers.append(f"{label}: {match.group(0)}")
    if identifiers:
        detail = "; ".join(identifiers)
    elif verification.status == "missing":
        detail = (
            f"{entry.document_name} was not found after full-corpus bidder evidence search."
            if entry.search_coverage.get("verification_search_exhausted")
            else f"{entry.document_name} was not found in the selected bidder evidence."
        )
    else:
        detail = provider_neutral_reason(verification.reason)
        if len(detail) > 280:
            detail = f"{detail[:277].rstrip()}..."
    source_note = f" Source: {source}." if source else ""
    return normalize_status_reason(verification.status, f"{detail}{source_note}")


_STATUS_REASON_PREFIX = re.compile(
    r"^\s*(?:pass(?:ed)?|fail(?:ed)?|missing|needs\s+(?:manual\s+)?review|unclear|not\s+evaluated|not\s+applicable)"
    r"\b\s*(?:[.:\-]\s*)?",
    flags=re.IGNORECASE,
)
_STATUS_REASON_LABELS = {
    "pass": "Passed",
    "passed": "Passed",
    "qualified": "Passed",
    "compliant": "Passed",
    "meets_requirement": "Passed",
    "fail": "Failed",
    "failed": "Failed",
    "does_not_meet": "Failed",
    "missing": "Missing",
    "unclear": "Needs review",
    "needs_review": "Needs review",
    "needs_manual_review": "Needs review",
    "review": "Needs review",
    "not_evaluated": "Not evaluated",
    "not_applicable": "Not applicable",
}
_STATUS_REASON_FALLBACKS = {
    "pass": "All required evidence checks passed.",
    "passed": "All required evidence checks passed.",
    "fail": "A required evidence check failed.",
    "failed": "A required evidence check failed.",
    "does_not_meet": "A required evidence check failed.",
    "missing": "Required evidence was not found.",
    "unclear": "The available evidence did not support a conclusive automated decision.",
    "needs_review": "The available evidence did not support a conclusive automated decision.",
    "needs_manual_review": "The available evidence did not support a conclusive automated decision.",
    "review": "The available evidence did not support a conclusive automated decision.",
    "not_evaluated": "This criterion was not evaluated.",
    "not_applicable": "The requirement does not apply to this bidder.",
}


def normalize_status_reason(status: Any, reason: Any) -> str:
    """Keep a stored explanation aligned with the status shown beside it."""
    normalized_status = str(status or "").strip().casefold()
    detail = _STATUS_REASON_PREFIX.sub("", provider_neutral_reason(reason)).strip()
    if not detail:
        detail = _STATUS_REASON_FALLBACKS.get(normalized_status, "Evidence review did not return an explanation.")
    label = _STATUS_REASON_LABELS.get(normalized_status)
    return f"{label}: {detail}" if label else detail


def provider_neutral_reason(value: Any) -> str:
    text = clean_text(str(value or ""))
    text = re.sub(
        r"^(?:(?:gemini|ai|model)\s+)?(?:criterion\s+)?(?:judge|finding|assessment)\s*[:\-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\bGemini\b", "Evidence review", text, flags=re.IGNORECASE)


def finding_is_processing_incomplete(finding: CriteriaFinding) -> bool:
    reason = normalize_for_match(finding.reason)
    return any(
        marker in reason
        for marker in (
            "complete semantic search coverage", "search incomplete", "remain uninspected",
            "provider unavailable", "provider timed out", "unreadable page",
        )
    )


def enforce_server_validation(
    report: EvaluationReport,
    job_id: str,
    docs: list[ExtractedDocument],
) -> EvaluationReport:
    report.job_id = job_id
    report.documents = [doc.record for doc in docs]

    mandatory_failures: list[str] = []
    # Canonical ledgers retain the raw reason for an unclear finding.  Rebuild
    # those finding-level messages below with the criterion ID so one issue is
    # not presented twice under two different strings.
    raw_finding_reasons = {
        normalize_for_match(finding.reason)
        for finding in report.findings
        if finding.status == "unclear" and finding.reason
    }
    manual_review_reasons = [
        reason
        for reason in report.manual_review_reasons
        if normalize_for_match(reason) not in raw_finding_reasons
    ]
    processing_incomplete_reasons: list[str] = []
    criterion_checks: list[dict[str, Any]] = []
    criteria_by_id = {criterion.criterion_id: criterion for criterion in report.tender.criteria}
    findings_by_id: dict[str, list[CriteriaFinding]] = {}
    for finding in report.findings:
        findings_by_id.setdefault(finding.criterion_id, []).append(finding)

    for criterion in report.tender.criteria:
        if is_post_award_criterion(criterion):
            criterion_checks.append({"criterion_id": criterion.criterion_id, "covered": True, "status": "post_award_not_gating"})
            continue
        matches = findings_by_id.get(criterion.criterion_id, [])
        if not matches:
            message = f"{criterion.criterion_id}: criterion was not evaluated"
            # Missing evaluator output is a system incompleteness, never proof
            # that the bidder failed the requirement.
            manual_review_reasons.append(message)
            criterion_checks.append({"criterion_id": criterion.criterion_id, "covered": False})
            continue
        if len(matches) > 1:
            manual_review_reasons.append(f"{criterion.criterion_id}: duplicate findings returned")
        for finding in matches:
            finding.criterion_text = criterion.requirement
            finding.category = criterion.category
            finding.mandatory = criterion.mandatory
            if not finding.required_documents:
                finding.required_documents = criterion.required_documents
            criterion_checks.append({"criterion_id": criterion.criterion_id, "covered": True, "status": finding.status})

    for finding in report.findings:
        criterion = criteria_by_id.get(finding.criterion_id)
        if not criterion:
            manual_review_reasons.append(f"{finding.criterion_id}: model returned a criterion not present in tender profile")
        elif is_post_award_criterion(criterion):
            continue
        if finding.status == "unclear" and finding_is_processing_incomplete(finding):
            finding.status = "not_evaluated"
            processing_incomplete_reasons.append(f"{finding.criterion_id}: {finding.reason}")
        if finding.status == "fail" and failure_is_missing_proof(finding):
            finding.status = "missing"
            if not finding.missing_documents:
                finding.missing_documents = list(finding.required_documents)
        if finding.mandatory and finding.status == "fail":
            if criterion and criterion.clarifiable:
                manual_review_reasons.append(f"{finding.criterion_id}: criterion depends on incomplete tender detail - {finding.reason}")
            else:
                mandatory_failures.append(f"{finding.criterion_id}: {finding.reason}")
        elif finding.mandatory and finding.status == "missing":
            manual_review_reasons.append(
                f"{finding.criterion_id}: required bidder document or evidence is missing - {finding.reason}"
            )
        elif finding.mandatory and finding.status == "unclear":
            manual_review_reasons.append(f"{finding.criterion_id}: mandatory criterion unclear - {finding.reason}")
        elif finding.mandatory and finding.status == "pass" and criterion and criterion.clarifiable:
            finding.status = "unclear"
            finding.reason = f"Criterion depends on incomplete tender detail; {finding.reason}"
            manual_review_reasons.append(f"{finding.criterion_id}: {finding.reason}")

    doc_index = {doc.record.document_id: doc for doc in docs}
    visually_verified = canonical_visual_evidence(report)
    for finding in report.findings:
        if finding.status != "pass":
            continue
        evidence_check = validate_finding_evidence(
            finding,
            doc_index,
            visually_verified=visually_verified.get(finding.criterion_id, set()),
        )
        if finding.mandatory and not evidence_check["has_vendor_submission"]:
            manual_review_reasons.append(
                f"{finding.criterion_id}: proposed pass has no valid vendor-submission evidence"
            )
        elif finding.mandatory and not evidence_check["has_quote_match"]:
            manual_review_reasons.append(f"{finding.criterion_id}: pass evidence quote could not be verified")
        if finding.mandatory and evidence_check["unverified_reason_page_ids"]:
            cited = ", ".join(evidence_check["unverified_reason_page_ids"])
            manual_review_reasons.append(
                f"{finding.criterion_id}: decision cites page(s) not attached as verified evidence - {cited}"
            )
    if report.missing_mandatory:
        manual_review_reasons.extend(
            f"Required bidder document or evidence is missing: {document}"
            for document in report.missing_mandatory
        )
    if not report.tender.criteria:
        manual_review_reasons.append("No tender criteria could be extracted.")

    evidence_document_ids = finding_evidence_document_ids(
        [
            finding
            for finding in report.findings
            if finding.mandatory and finding.status != "pass"
        ]
    )
    for document in docs:
        if document.record.document_id not in evidence_document_ids:
            continue
        for page in document.pages:
            if page.ocr_required:
                manual_review_reasons.append(f"{page.file_name} page {page.page_number}: evidence-related OCR required but not resolved")

    if mandatory_failures:
        report.qualifies = False
        report.recommendation = "rejected"
        report.responsiveness.status = "unresponsive"
        report.responsiveness.strict_fail_reasons = sorted(set(mandatory_failures))
        report.financial_result.status = "not_opened"
        report.financial_result.opening_allowed = False
        report.final_decision = "reject_unresponsive"
    elif manual_review_reasons or processing_incomplete_reasons:
        report.qualifies = False
        report.recommendation = "needs_manual_review"
        report.responsiveness.status = "clarification_required"
        report.financial_result.status = "not_opened"
        report.financial_result.opening_allowed = False
        report.final_decision = "needs_manual_review"
    else:
        report.qualifies = True
        report.recommendation = "technically_qualified"
        report.responsiveness.status = "responsive"
        report.financial_result.opening_allowed = True
        report.final_decision = "financial_opening_allowed"

    report.manual_review_reasons = sorted(set(manual_review_reasons))
    report.validation = {
        "schema_valid": True,
        "strict_mandatory_fail_applied": True,
        "mandatory_failures": sorted(set(mandatory_failures)),
        "manual_review_reasons": report.manual_review_reasons,
        "processing_incomplete_reasons": sorted(set(processing_incomplete_reasons)),
        "criterion_checks": criterion_checks,
        "procurement_sources": PROCUREMENT_SOURCES,
        # ponytail: file-backed job snapshots fit this single-worker deployment; use DB storage behind multiple workers.
        "job_storage": "file",
    }
    return report


def failure_is_missing_proof(finding: CriteriaFinding) -> bool:
    """An absent or uncompleted artifact is reviewable missing proof, not contradictory proof."""
    text = normalize_for_match(finding.reason)
    return any(
        phrase in text
        for phrase in (
            "not submitted",
            "not provided",
            "not found",
            "was not found",
            "blank and unexecuted",
            "blank or unexecuted",
            "unsigned",
            "not duly filled",
            "required document is missing",
            "required evidence is missing",
            "failed required check s presence",
        )
    )


def finding_evidence_document_ids(findings: list[CriteriaFinding]) -> set[str]:
    return {
        evidence.document_id
        for finding in findings
        for evidence in finding.evidence
        if evidence.document_id
    }


def canonical_visual_evidence(report: EvaluationReport) -> dict[str, set[tuple[str, int]]]:
    """Return exact pages the canonical ledger records as directly image-inspected."""
    payload = ((report.extensions or {}).get("canonical_decision_ledger") or {}).get("payload") or {}
    verified: dict[str, set[tuple[str, int]]] = {}
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        criterion_id = str(result.get("criterion_id") or "").strip()
        assessment = result.get("effective_assessment") or result.get("ai_assessment") or {}
        if not criterion_id or not isinstance(assessment, dict):
            continue
        for ref in assessment.get("evidence_refs") or []:
            if not isinstance(ref, dict) or not str(ref.get("image_ref") or "").strip():
                continue
            document_id = str(ref.get("document_id") or "").strip()
            try:
                page = int(ref.get("page") or 0)
            except (TypeError, ValueError):
                continue
            if document_id and page > 0:
                verified.setdefault(criterion_id, set()).add((document_id, page))
    return verified


def validate_finding_evidence(
    finding: CriteriaFinding,
    doc_index: dict[str, ExtractedDocument],
    *,
    visually_verified: set[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    has_vendor_submission = False
    has_quote_match = False
    visual_pages = visually_verified or set()
    supported_pages: set[tuple[str, int]] = set()
    for evidence in finding.evidence:
        if not evidence.document_id:
            continue
        document = doc_index.get(evidence.document_id)
        if not document:
            continue
        if not is_vendor_submission_document(document):
            continue
        has_vendor_submission = True
        if evidence.page and (evidence.document_id, evidence.page) in visual_pages:
            has_quote_match = True
            supported_pages.add((evidence.document_id, evidence.page))
        elif evidence.quote and quote_matches_text(evidence.quote, document.text):
            has_quote_match = True
            if evidence.page and quote_matches_page(evidence.quote, document, evidence.page):
                supported_pages.add((evidence.document_id, evidence.page))

    known_pages = {
        page.page_id: (document.record.document_id, page.page_number)
        for document in doc_index.values()
        for page in document.pages
        if page.page_id
    }
    unverified_reason_page_ids = sorted(
        page_id
        for page_id, page_ref in known_pages.items()
        if page_id_is_cited(finding.reason, page_id) and page_ref not in supported_pages
    )
    return {
        "has_vendor_submission": has_vendor_submission,
        "has_quote_match": has_quote_match,
        "unverified_reason_page_ids": unverified_reason_page_ids,
    }


def quote_matches_page(quote: str, document: ExtractedDocument, page_number: int) -> bool:
    for page in document.pages:
        if page.page_number == page_number:
            page_text = page.text or page.layout_text
            return bool(page_text) and quote_matches_text(quote, page_text)
    return False


def page_id_is_cited(reason: str, page_id: str) -> bool:
    return bool(
        reason
        and page_id
        and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(page_id)}(?![A-Za-z0-9])",
            reason,
            flags=re.IGNORECASE,
        )
    )


def is_vendor_submission_document(document: ExtractedDocument) -> bool:
    classification = document.record.classification
    if document.record.upload_side != "vendor" or document.record.extraction_status != "ok":
        return False
    if classification and classification.content_role == "tender_source":
        return False
    if classification and classification.content_role == "reference":
        # A vendor packet can legitimately contain independent third-party
        # evidence (inspection reports, certificates, client references). Only
        # exclude it when the content itself is a copied tender/source page.
        return not tender_source_trap(normalize_for_match(document.text))
    return True
