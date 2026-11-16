"""Deterministic criterion assessment and evidence rollups."""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from procurement.domain.schemas import (
    CriteriaFinding,
    CriterionValidatorResult,
    CriterionValidatorSet,
    DocumentVerification,
    DocumentVerificationSet,
    Evidence,
    EvidenceLedger,
    EvidenceLedgerChunk,
    EvidencePacketSet,
    EvaluationReport,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
    RequirementMatrix,
    RequirementMatrixItem,
    TenderProfile,
    VendorProfile,
)
from procurement.domain.text import clean_text, humanize_check_name, normalize_for_match
from procurement.domain.text import keyword_tokens
from procurement.evaluation.criteria import (
    criterion_is_conditionally_worded,
    criterion_kind_signal_score,
    criterion_signal_terms,
    criterion_validator_kinds,
)
from procurement.evaluation.evidence import EvidenceLedger as EntityEvidenceLedger
from procurement.evaluation.facts import bidder_identifier_evidence_ledger
from procurement.evaluation.validators import validate_criterion_deterministically
from procurement.understanding.classification import tender_source_trap
from procurement.understanding.quality import MAX_DOC_CHARS


DETERMINISTIC_CRITERION_VALIDATORS_ENABLED = os.getenv("DETERMINISTIC_CRITERION_VALIDATORS_ENABLED", "1") != "0"


def page_text_index(docs: list[ExtractedDocument]) -> dict[str, str]:
    return {
        page.page_id: clean_text(
            f"[page {page.page_number}]\n{page.text}"
        )
        for document in docs
        for page in document.pages
    }


def ledger_chunk_texts(ledger: EvidenceLedger, docs: list[ExtractedDocument]) -> dict[str, str]:
    page_text_by_id = page_text_index(docs)
    return {
        chunk.chunk_id: clean_text("\n\n".join(page_text_by_id.get(page_id, "") for page_id in chunk.page_ids))
        for chunk in ledger.chunks
    }


def deterministic_criterion_validator_agent(
    matrix: RequirementMatrix,
    findings: list[CriteriaFinding],
    verification_set: DocumentVerificationSet,
    evidence_packets: EvidencePacketSet,
    evidence_ledger: EvidenceLedger,
    docs: list[ExtractedDocument],
) -> CriterionValidatorSet:
    if not DETERMINISTIC_CRITERION_VALIDATORS_ENABLED:
        return CriterionValidatorSet(coverage_summary={"enabled": False})

    chunk_text_by_id = ledger_chunk_texts(evidence_ledger, docs)
    bidder_identifiers = bidder_identifier_evidence_ledger(docs)
    verifications_by_criterion: dict[str, list[DocumentVerification]] = {}
    for verification in verification_set.items:
        verifications_by_criterion.setdefault(verification.criterion_id, []).append(verification)
    results: list[CriterionValidatorResult] = []
    for item in matrix.items:
        candidate = criterion_candidate_evidence(
            item,
            evidence_packets,
            evidence_ledger,
            chunk_text_by_id,
            bidder_identifiers=bidder_identifiers,
            verifications=verifications_by_criterion.get(item.criterion_id, []),
        )
        results.append(validate_criterion_deterministically(item, candidate))

    actionable = [result for result in results if result.status != "unchanged"]
    return CriterionValidatorSet(
        results=results,
        coverage_summary={
            "enabled": True,
            "criteria_checked": len(matrix.items),
            "actionable_results": len(actionable),
            "ledger_chunks": len(evidence_ledger.chunks),
            "validator_statuses": {
                status: sum(1 for result in actionable if result.status == status)
                for status in ["pass", "fail", "missing", "unclear"]
            },
        },
    )


def criterion_candidate_evidence(
    item: RequirementMatrixItem,
    evidence_packets: EvidencePacketSet,
    evidence_ledger: EvidenceLedger,
    chunk_text_by_id: dict[str, str],
    *,
    bidder_identifiers: EntityEvidenceLedger | None = None,
    verifications: list[DocumentVerification] | None = None,
) -> dict[str, Any]:
    packet_texts = [
        packet.text
        for packet in evidence_packets.packets
        if packet.criterion_id == item.criterion_id and packet.text
    ]
    kinds = criterion_validator_kinds(item)
    terms = criterion_signal_terms(item)
    scored_chunks: list[tuple[int, EvidenceLedgerChunk, str]] = []
    for chunk in evidence_ledger.chunks:
        if chunk.upload_side != "vendor" or chunk.content_role == "tender_source":
            continue
        text = chunk_text_by_id.get(chunk.chunk_id, "")
        normalized = normalize_for_match(text)
        if not normalized or tender_source_trap(normalized):
            continue
        score = 10 if item.criterion_id in chunk.matched_criteria else 0
        score += len(terms & keyword_tokens(normalized))
        score += criterion_kind_signal_score(kinds, normalized)
        if score:
            scored_chunks.append((score, chunk, text))

    scored_chunks.sort(key=lambda row: (-row[0], row[1].document_id, row[1].page_start))
    selected = scored_chunks[:4]
    verification_texts = [
        verification.quote or verification.reason
        for verification in verifications or []
        if verification.status != "missing" and (verification.quote or verification.reason)
    ]
    texts = packet_texts + verification_texts + [text for _, _, text in selected]
    documents = {chunk.file_name for _, chunk, _ in selected}
    document_ids = {chunk.document_id for _, chunk, _ in selected}
    page_ids = {
        page_id
        for _, chunk, _ in selected
        for page_id in chunk.page_ids
    } | {
        page_id
        for verification in verifications or []
        if verification.status != "missing"
        for page_id in verification.page_ids
    }
    return {
        "text": clean_text("\n\n".join(texts))[:MAX_DOC_CHARS],
        "documents": sorted(value for value in documents if value),
        "document_ids": sorted(value for value in document_ids if value),
        "page_ids": sorted(value for value in page_ids if value),
        "chunks_checked": len(scored_chunks),
        "kinds": kinds,
        "bidder_identifier_ledger": bidder_identifiers,
    }


def criteria_evaluator_agent(
    matrix: RequirementMatrix,
    verification_set: DocumentVerificationSet,
    logical_documents: list[LogicalDocument],
    apply_rollups: bool = True,
    docs: list[ExtractedDocument] | None = None,
) -> list[CriteriaFinding]:
    by_criterion: dict[str, list[DocumentVerification]] = {}
    for item in verification_set.items:
        by_criterion.setdefault(item.criterion_id, []).append(item)
    logical_to_source = {
        logical.logical_document_id: logical.source_document_ids
        for logical in logical_documents
    }
    page_text_by_id = page_text_index(docs) if docs is not None else None

    findings: list[CriteriaFinding] = []
    for item in matrix.items:
        verifications = by_criterion.get(item.criterion_id, [])
        required_ids = [evidence.evidence_id for evidence in item.required_evidence if evidence.required]
        required_results = [verification for verification in verifications if verification.evidence_id in required_ids]
        groups = requirement_evidence_groups(item)
        evidence_by_id = {evidence.evidence_id: evidence for evidence in item.required_evidence}
        group_results = evaluate_evidence_groups(groups, verifications, evidence_by_id)
        missing_documents = [group["label"] for group in group_results if group["status"] == "missing"]

        if not groups and not verifications:
            status = "not_evaluated" if not item.mandatory else "missing"
            reason = "No bidder evidence was evaluated for this criterion."
            missing_documents = [evidence.document_name for evidence in item.required_evidence if evidence.required]
        elif not groups:
            status, reason = status_reason_for_verifications(verifications, evidence_by_id)
        elif not required_results and required_ids:
            status = "missing"
            reason = "No required evidence documents were found."
            missing_documents = [evidence.document_name for evidence in item.required_evidence if evidence.required]
        elif group_results and all(group["status"] == "pass" for group in group_results):
            status = "pass"
            reason = "; ".join(
                verification.reason
                for verification in required_results
                if verification.reason
            ) or "All required evidence checks passed."
        elif any(group["status"] == "unclear" for group in group_results):
            status = "unclear"
            reason = "; ".join(group["reason"] for group in group_results if group["status"] == "unclear")
        elif any(group["status"] == "fail" for group in group_results):
            status = "fail"
            reason = "; ".join(group["reason"] for group in group_results if group["status"] == "fail")
        elif any(group["status"] == "missing" for group in group_results):
            status = "missing"
            reason = "; ".join(group["reason"] for group in group_results if group["status"] == "missing")
        else:
            status = "pass"
            reason = "All required evidence checks passed."

        applicability = str((item.applicability or {}).get("state") or "applicable")
        if applicability == "applicable" and criterion_is_conditionally_worded(item):
            applicability = "conditional"
        applicability_rule = str((item.applicability or {}).get("rule") or "").strip()
        if applicability == "not_applicable":
            status = "not_applicable"
            reason = "The tender marks this requirement as not applicable."
            missing_documents = []
            applicability_status = "not_applicable"
            applicability_reason = reason
        elif applicability == "conditional":
            applicability_status = "unclear"
            applicability_reason = (
                f"The bidder-specific trigger has not yet been resolved: {applicability_rule}"
                if applicability_rule
                else "The bidder-specific applicability trigger has not yet been resolved."
            )
            if status != "pass":
                status = "unclear"
                reason = applicability_reason
        else:
            applicability_status = "applicable"
            applicability_reason = "The tender applies this requirement to every bidder."

        matched_documents = sorted(
            {
                source_id
                for result in verifications
                for logical_id in result.logical_document_ids
                for source_id in logical_to_source.get(logical_id, [])
            }
        )
        findings.append(
            CriteriaFinding(
                criterion_id=item.criterion_id,
                criterion_text=item.requirement,
                category=item.category,
                mandatory=item.mandatory,
                status=status,
                reason=reason or "Evaluated from requirement matrix.",
                required_documents=[evidence.document_name for evidence in item.required_evidence if evidence.required],
                missing_documents=missing_documents if status == "missing" else [],
                matched_documents=matched_documents,
                evidence=[
                    evidence
                    for result in verifications
                    for evidence in evidence_rows_for_verification(
                        result,
                        logical_to_source,
                        page_text_by_id,
                    )
                ],
                criterion_kind=item.criterion_kind,
                evaluation_scope=item.evaluation_scope,
                materiality=item.materiality,
                decision_effect=item.decision_effect,
                applicability_status=applicability_status,
                applicability_reason=applicability_reason,
            )
        )
    if apply_rollups:
        apply_rollup_and_downstream_statuses(findings, matrix.items)
    return findings


def enforce_required_evidence_gates(
    findings: list[CriteriaFinding],
    matrix: RequirementMatrix,
    verification_set: DocumentVerificationSet,
) -> None:
    """A downstream judge cannot pass a criterion whose required proof did not pass."""
    findings_by_id = {finding.criterion_id: finding for finding in findings}
    verifications_by_criterion: dict[str, list[DocumentVerification]] = {}
    for verification in verification_set.items:
        verifications_by_criterion.setdefault(verification.criterion_id, []).append(verification)
    for item in matrix.items:
        finding = findings_by_id.get(item.criterion_id)
        groups = requirement_evidence_groups(item)
        if not finding or finding.status != "pass" or not groups:
            continue
        evidence_by_id = {evidence.evidence_id: evidence for evidence in item.required_evidence}
        contradiction = next(
            (
                verification
                for verification in verifications_by_criterion.get(item.criterion_id, [])
                if verification.status == "pass"
                and positive_submission_quote_contradicts(
                    item,
                    evidence_by_id.get(verification.evidence_id),
                    verification.quote,
                )
            ),
            None,
        )
        if contradiction:
            finding.status = "fail"
            finding.reason = "The bidder's submitted checklist marks the required document as not enclosed."
            finding.missing_documents = [
                evidence_by_id[contradiction.evidence_id].document_name
            ]
            continue
        if any(
            verification.status == "pass" and referral_only_quote(item, verification.quote)
            for verification in verifications_by_criterion.get(item.criterion_id, [])
        ):
            finding.status = "unclear"
            finding.reason = (
                "The response only refers to another document; the required list or details "
                "were not verified on the cited evidence page."
            )
            finding.missing_documents = []
            continue
        results = evaluate_evidence_groups(
            groups,
            verifications_by_criterion.get(item.criterion_id, []),
            evidence_by_id,
        )
        if results and all(result["status"] == "pass" for result in results):
            continue
        failed = next((result for result in results if result["status"] == "fail"), None)
        missing = next((result for result in results if result["status"] == "missing"), None)
        unclear = next((result for result in results if result["status"] == "unclear"), None)
        finding.status = "fail" if failed else "missing" if missing else "unclear"
        finding.reason = (failed or missing or unclear or next(iter(results), {})).get(
            "reason",
            "Required bidder evidence was not verified.",
        )
        finding.missing_documents = [
            result["label"] for result in results if result["status"] == "missing"
        ]


def positive_submission_quote_contradicts(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement | None,
    quote: str | None,
) -> bool:
    if not evidence or not quote:
        return False
    if not re.search(
        r"\b(?:submit|provide|upload|enclose|attach|furnish)\b",
        normalize_for_match(item.requirement),
    ):
        return False
    evidence_terms = keyword_tokens(evidence.document_name)
    for line in str(quote).splitlines():
        if not re.search(
            r"\|\s*(?:no|not submitted|not enclosed)\s*\|?\s*$",
            line,
            re.IGNORECASE,
        ):
            continue
        if len(evidence_terms.intersection(keyword_tokens(line))) >= min(2, len(evidence_terms)):
            return True
    return False


def referral_only_quote(item: RequirementMatrixItem, quote: str | None) -> bool:
    if not quote:
        return False
    requirement = normalize_for_match(f"{item.requirement} {item.pass_rule}")
    requires_contents = bool(
        re.search(r"\b(?:submit|provide|specify|detail)\b", requirement)
        and re.search(r"\b(?:list|details|specification|schedule|drawing)\b", requirement)
    )
    normalized_quote = normalize_for_match(quote)
    return requires_contents and bool(
        re.fullmatch(
            r"(?:details of |list of )?.{0,120}\b(?:refer|see)\s+(?:the\s+)?"
            r"(?:catalogue|catalog|annexure|attachment)\b.{0,40}",
            normalized_quote,
        )
    )


def apply_positive_rollup_statuses(findings: list[CriteriaFinding], items: list[RequirementMatrixItem]) -> None:
    item_by_id = {item.criterion_id: item for item in items}
    technical_ids = {
        finding.criterion_id
        for finding in findings
        if is_technical_acceptance_rollup(item_by_id.get(finding.criterion_id, RequirementMatrixItem(criterion_id=finding.criterion_id, requirement=finding.criterion_text)))
    }
    if not technical_ids:
        return
    has_blocker = any(
        finding.mandatory
        and finding.criterion_id not in technical_ids
        and finding.status in {"fail", "missing"}
        for finding in findings
    )
    if has_blocker:
        return
    for finding in findings:
        if (
            finding.criterion_id in technical_ids
            and not finding.mandatory
            and finding.status in {"missing", "unclear", "not_evaluated"}
        ):
            finding.status = "pass"
            finding.reason = "Technical acceptance rollup passed because no mandatory technical/BQC blocker remains."
            finding.missing_documents = []


def status_reason_for_verifications(
    verifications: list[DocumentVerification],
    evidence_by_id: dict[str, EvidenceRequirement],
) -> tuple[Literal["pass", "fail", "missing", "unclear", "not_evaluated"], str]:
    statuses = [criteria_status_for_verification(verification) for verification in verifications]
    if "fail" in statuses:
        return "fail", "; ".join(
            verification_group_reason(verification, evidence_by_id.get(verification.evidence_id))
            for verification in verifications
            if criteria_status_for_verification(verification) == "fail"
        )
    if "unclear" in statuses:
        return "unclear", "; ".join(
            verification.reason
            for verification in verifications
            if criteria_status_for_verification(verification) == "unclear"
        )
    if "missing" in statuses:
        return "not_evaluated", "; ".join(
            verification_group_reason(verification, evidence_by_id.get(verification.evidence_id))
            for verification in verifications
            if criteria_status_for_verification(verification) == "missing"
        )
    if "pass" in statuses:
        return "pass", "All required evidence checks passed."
    return "not_evaluated", "No bidder evidence was evaluated for this criterion."


def apply_rollup_and_downstream_statuses(findings: list[CriteriaFinding], items: list[RequirementMatrixItem]) -> None:
    item_by_id = {item.criterion_id: item for item in items}
    has_mandatory_blocker = any(finding.mandatory and finding.status in {"fail", "missing"} for finding in findings)
    if not has_mandatory_blocker:
        return
    for finding in findings:
        item = item_by_id.get(finding.criterion_id)
        if not item or finding.mandatory:
            continue
        if is_technical_acceptance_rollup(item):
            finding.status = "fail"
            finding.reason = "Bidder was not technically accepted because mandatory technical/BQC criteria failed."


def is_technical_acceptance_rollup(item: RequirementMatrixItem) -> bool:
    text = normalize_for_match(item.requirement)
    return "technically accepted" in text and "evaluation" in text


def uses_any_of_evidence(item: RequirementMatrixItem) -> bool:
    if any(len(group) > 1 for group in requirement_evidence_groups(item)):
        return True
    text = f"{item.requirement} {item.pass_rule}".lower()
    evidence_names = " ".join(evidence.document_name for evidence in item.required_evidence).lower()
    return (
        " any " in f" {text} "
        or " or " in f" {text} "
        or "either" in text
        or " or " in f" {evidence_names} "
    )


def requirement_evidence_groups(item: RequirementMatrixItem) -> list[list[str]]:
    required = [evidence for evidence in item.required_evidence if evidence.required]
    if not required:
        return []
    evaluation = (item.extensions or {}).get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    configured = evaluation.get("evidence_groups")
    if isinstance(configured, list):
        valid_ids = {evidence.evidence_id for evidence in required}
        groups: list[list[str]] = []
        seen: set[str] = set()
        for raw_group in configured:
            if not isinstance(raw_group, list):
                continue
            group = [
                str(evidence_id)
                for evidence_id in raw_group
                if str(evidence_id) in valid_ids and str(evidence_id) not in seen
            ]
            if group:
                groups.append(group)
                seen.update(group)
        groups.extend(
            [[evidence.evidence_id] for evidence in required if evidence.evidence_id not in seen]
        )
        if groups:
            criterion_text = normalize_for_match(
                f"{item.requirement} {item.pass_rule} {item.source_quote or ''}"
            )
            explicit_alternative = bool(
                re.search(r"\b(?:or|either|one of|any one of|any of)\b", criterion_text)
            )
            if len(groups) == 1 and len(groups[0]) > 1 and not explicit_alternative:
                return [[evidence_id] for evidence_id in groups[0]]
            return groups
    grouped: dict[str, list[str]] = {}
    standalone: list[list[str]] = []
    for evidence in required:
        group_key = procurement_alternative_group(evidence.document_name, item)
        if group_key:
            grouped.setdefault(group_key, []).append(evidence.evidence_id)
        else:
            standalone.append([evidence.evidence_id])
    groups = standalone[:]
    groups.extend(group for group in grouped.values())
    return groups


def procurement_alternative_group(document_name: str, item: RequirementMatrixItem) -> str | None:
    name = document_name.lower()
    text = f"{item.requirement} {item.pass_rule} {item.source_quote or ''} {document_name}".lower()
    emd_context = any(
        term in f"{item.requirement} {item.source_quote or ''}".lower()
        for term in ["emd", "earnest money", "bid security"]
    )
    if emd_context and any(
        term in name
        for term in [
            "bank guarantee", "demand draft", "pay order", "bid security declaration",
            "udyam", "mse", "msme", "exemption",
        ]
    ):
        return "bid_security_or_exemption"
    if any(term in name for term in ["udyam", "mse", "msme", "exemption"]) or any(
        term in name for term in ["emd", "earnest money", "bid security"]
    ):
        if " or " in text or "exemption" in text:
            return "bid_security_or_exemption"
    if any(term in name for term in ["completion certificate", "tax invoice", "invoice", "payment receipt"]):
        return "execution_proof"
    if "turnover" in text and any(term in name for term in ["balance sheet", "audited", "ca certificate", "turnover certificate"]):
        return "financial_turnover_proof"
    return None


def evaluate_evidence_groups(
    groups: list[list[str]],
    verifications: list[DocumentVerification],
    evidence_by_id: dict[str, EvidenceRequirement],
) -> list[dict[str, str]]:
    by_evidence = {verification.evidence_id: verification for verification in verifications}
    results: list[dict[str, str]] = []
    for group in groups:
        group_verifications = [by_evidence[evidence_id] for evidence_id in group if evidence_id in by_evidence]
        label = " or ".join(evidence_by_id[evidence_id].document_name for evidence_id in group if evidence_id in evidence_by_id)
        statuses = [criteria_status_for_verification(verification) for verification in group_verifications]
        if "pass" in statuses:
            results.append({"status": "pass", "reason": "At least one accepted evidence item passed.", "label": label})
        elif "unclear" in statuses:
            results.append(
                {
                    "status": "unclear",
                    "label": label,
                    "reason": "; ".join(
                        verification.reason
                        for verification in group_verifications
                        if criteria_status_for_verification(verification) == "unclear"
                    ),
                }
            )
        elif "fail" in statuses:
            results.append(
                {
                    "status": "fail",
                    "label": label,
                    "reason": "; ".join(
                        verification_group_reason(verification, evidence_by_id.get(verification.evidence_id))
                        for verification in group_verifications
                        if criteria_status_for_verification(verification) == "fail"
                    ),
                }
            )
        else:
            reason = "; ".join(
                verification_group_reason(verification, evidence_by_id.get(verification.evidence_id))
                for verification in group_verifications
                if verification.reason or verification.check_results
            )
            results.append({"status": "missing", "reason": reason or "Required evidence group missing.", "label": label})
    return results


def verification_group_reason(verification: DocumentVerification, evidence: EvidenceRequirement | None = None) -> str:
    failed = [
        key
        for key, value in verification.check_results.items()
        if str(value).lower() in {"fail", "missing"} and key != "specialist_content"
    ]
    if failed:
        document = humanize_check_name(evidence.document_name) if evidence else "Required evidence"
        checks = ", ".join(humanize_check_name(check) for check in failed)
        if verification.status == "missing" and not verification.logical_document_ids:
            return f"Missing: {document}."
        if verification.logical_document_ids and any(str(verification.check_results.get(check)).lower() == "missing" for check in failed):
            return f"{document}: submitted evidence did not prove the required document/checks."
        if any(str(verification.check_results.get(check)).lower() == "missing" for check in failed):
            return f"{document}: missing or unverified required check(s): {checks}."
        return f"{document}: failed required check(s): {checks}."
    return verification.reason or "Evidence check did not pass."


def criteria_status_for_verification(verification: DocumentVerification) -> str:
    if verification.status == "missing" and verification.logical_document_ids:
        return "fail"
    if verification.status == "fail" and presence_only_failure(verification):
        return "missing"
    if verification.status == "fail" and signature_stamp_only_failure(verification):
        return "unclear"
    return verification.status


def presence_only_failure(verification: DocumentVerification) -> bool:
    failed_or_missing = {
        key.lower()
        for key, value in verification.check_results.items()
        if str(value).lower() in {"fail", "missing"}
    }
    return bool(failed_or_missing) and failed_or_missing <= {"presence"}


def signature_stamp_only_failure(verification: DocumentVerification) -> bool:
    failed = {key.lower() for key, value in verification.check_results.items() if str(value).lower() == "fail"}
    normalized = {
        key
        for key in failed
        if key in {"signed", "signature", "stamped", "stamp", "seal", "letterhead", "bidder_letterhead"}
        or "sign" in key
        or "stamp" in key
        or "seal" in key
        or "letterhead" in key
    }
    return bool(failed) and failed == normalized


def source_ids_for_verification(
    verification: DocumentVerification,
    logical_to_source: dict[str, list[str]],
) -> list[str]:
    source_ids: list[str] = []
    for logical_id in verification.logical_document_ids:
        for source_id in logical_to_source.get(logical_id, []):
            if source_id not in source_ids:
                source_ids.append(source_id)
    return source_ids


def evidence_rows_for_verification(
    verification: DocumentVerification,
    logical_to_source: dict[str, list[str]],
    page_text_by_id: dict[str, str] | None = None,
) -> list[Evidence]:
    source_ids = source_ids_for_verification(verification, logical_to_source)
    rows: list[Evidence] = []
    normalized_quote = normalize_for_match(verification.quote or "")
    for page_id in verification.page_ids:
        quote_matches = bool(
            normalized_quote
            and (
                page_text_by_id is None
                or normalized_quote in normalize_for_match(page_text_by_id.get(page_id, ""))
            )
        )
        if normalized_quote and not quote_matches:
            continue
        match = re.search(r"-p(\d+)$", page_id)
        document_id = next(
            (source_id for source_id in source_ids if page_id.startswith(f"{source_id}-p")),
            source_ids[0] if source_ids else None,
        )
        if not document_id or not match:
            continue
        rows.append(
            Evidence(
                document_id=document_id,
                page=int(match.group(1)),
                quote=verification.quote if quote_matches else None,
                confidence=0.8,
            )
        )
    if rows or page_text_by_id is not None:
        return rows
    return [
        Evidence(document_id=source_ids[0], quote=verification.quote, confidence=0.8)
    ] if source_ids and verification.quote else []


def decision_agent(
    job_id: str,
    tender: TenderProfile,
    vendor: VendorProfile,
    docs: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
    findings: list[CriteriaFinding],
    matrix: RequirementMatrix | None = None,
    verification_set: DocumentVerificationSet | None = None,
) -> EvaluationReport:
    missing = [
        missing_doc
        for finding in findings
        if finding.mandatory and finding.status == "missing"
        for missing_doc in (finding.missing_documents or finding.required_documents)
    ]
    failed = [
        f"{finding.criterion_id}: {finding.reason}"
        for finding in findings
        if finding.mandatory and finding.status == "fail"
    ]
    unclear = [
        f"{finding.criterion_id}: {finding.reason}"
        for finding in findings
        if finding.mandatory and finding.status == "unclear"
    ]
    recommendation: Literal["technically_qualified", "rejected", "needs_manual_review"]
    if missing or failed:
        recommendation = "rejected"
        summary = "Bid is non-responsive because mandatory evidence is missing or failed verification."
    elif unclear:
        recommendation = "needs_manual_review"
        summary = "Bid requires manual review because mandatory evidence could not be verified conclusively."
    else:
        recommendation = "technically_qualified"
        summary = "Mandatory evidence checks passed; financial opening may proceed subject to human review."
    report = EvaluationReport(
        job_id=job_id,
        procurement_source="gem" if any("gem" in doc.file_name.lower() for doc in [item.record for item in docs]) else "unknown",
        procurement_category=tender.procurement_type.lower() if tender.procurement_type else "unknown",
        evaluation_method=tender.evaluation_method or "unknown",
        qualifies=recommendation == "technically_qualified",
        recommendation=recommendation,
        summary=summary,
        tender=tender,
        vendor=vendor,
        documents=[doc.record for doc in docs],
        logical_documents=logical_documents,
        findings=findings,
        missing_mandatory=sorted(set(missing)),
        manual_review_reasons=unclear,
        risk_flags=unclear,
        final_decision="",
    )
    from procurement.evaluation.runtime import apply_ledger_to_report, build_runtime_ledger

    canonical_criteria: list[Any] = list(matrix.items if matrix is not None else tender.criteria)
    ledger = build_runtime_ledger(
        canonical_criteria,
        findings,
        documents=docs,
        verifications=list(verification_set.items if verification_set is not None else []),
        bid_id=job_id,
        metadata={"source": "reviewed_bidder_evaluation.v1",
                  "unautomated_requirements": list(matrix.extensions.get("unautomated_requirements") or []) if matrix is not None else []},
    )
    return apply_ledger_to_report(report, ledger)
