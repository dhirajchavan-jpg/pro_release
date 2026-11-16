"""Specialist evidence checks and cross-document reconciliation."""

from __future__ import annotations

import re
from typing import Any, Literal

from procurement.domain.schemas import (
    DocumentVerification,
    EvidenceMapEntry,
    EvidencePacket,
    EvidencePacketSet,
    EvidenceRequirement,
    RequirementMatrix,
    RequirementMatrixItem,
    SpecialistFinding,
    SpecialistFindingSet,
)
from procurement.domain.serialization import model_dump
from procurement.domain.text import normalize_for_match
from procurement.evaluation.facts import normalize_fact_value
from procurement.evaluation.verification import best_quote, status_from_check_results

from .evidence_pipeline_routing import _hook, evidence_marker_score
from .validators_compliance import emd_exemption_state, emd_payment_present


STRICT_EVIDENCE_CHECKS = {
    "average_formula",
    "bidder_name_match",
    "date_valid",
    "execution_proof",
    "figures_match",
    "financial_years",
    "item_similarity",
    "matches_po_reference",
    "signed",
    "stamped",
    "substantive_match",
    "threshold_value",
    "tender_requirement_match",
    "udin",
    "udin_if_ca",
    "value_threshold",
}


def annexure_labels(value: str) -> set[str]:
    labels: set[str] = set()
    for match in re.finditer(r"annexure\s*[-:]?\s*((?:\d+[a-z]?\s*){1,2})", value, flags=re.IGNORECASE):
        labels.update(re.findall(r"\d+[a-z]?", match.group(1).casefold()))
    return labels


def specialist_confirms_document_identity(entry: Any, finding: Any) -> bool:
    document_name = entry.get("document_name") if isinstance(entry, dict) else entry.document_name
    quote = finding.get("quote") if isinstance(finding, dict) else finding.quote
    expected = annexure_labels(str(document_name or ""))
    return bool(expected and expected & annexure_labels(str(quote or "")))


def _declaration_identity_terms(value: str) -> list[str]:
    ignored = {
        "annexure", "certificate", "declaration", "document", "documents", "form",
        "regarding", "submitted", "undertaking", "authorized", "authorised", "signatory",
        "the", "their", "with", "from", "under", "per", "and", "for", "as",
    }
    return [
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]+", normalize_for_match(value))
        if len(token) >= 4 and not token.isdigit() and token not in ignored
    ]


def declaration_subject_matches(document_name: str, text: str) -> bool:
    identity_terms = _declaration_identity_terms(document_name)
    required_matches = min(2, len(set(identity_terms)))
    matched = len({term for term in identity_terms if term in normalize_for_match(text)})
    expected_labels = annexure_labels(document_name)
    actual_labels = annexure_labels(text)
    return bool(
        (expected_labels & actual_labels and matched >= 1)
        or (required_matches and matched >= required_matches)
    )


def specialist_orchestrator_agent(
    matrix: RequirementMatrix,
    evidence_packets: EvidencePacketSet,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> SpecialistFindingSet:
    matrix_by_id = {item.criterion_id: item for item in matrix.items}
    evidence_by_key = {
        (item.criterion_id, evidence.evidence_id): evidence
        for item in matrix.items
        for evidence in item.required_evidence
    }
    run_specialist = _hook(compatibility_hooks, "run_specialist_agent", run_specialist_agent)
    findings = [
        run_specialist(
            packet,
            matrix_by_id.get(packet.criterion_id),
            evidence_by_key.get((packet.criterion_id, packet.evidence_id)),
        )
        for packet in evidence_packets.packets
    ]
    return SpecialistFindingSet(findings=findings)


def cross_document_reconciliation_agent(
    matrix: RequirementMatrix,
    evidence_packets: EvidencePacketSet,
    specialist_findings: SpecialistFindingSet,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> SpecialistFindingSet:
    by_criterion: dict[str, list[SpecialistFinding]] = {}
    for finding in specialist_findings.findings:
        by_criterion.setdefault(finding.criterion_id, []).append(finding)
    packet_by_id = {packet.packet_id: packet for packet in evidence_packets.packets}
    conflict_checker = _hook(compatibility_hooks, "cross_document_conflict", cross_document_conflict)
    additions: list[SpecialistFinding] = []
    for item in matrix.items:
        findings = [finding for finding in by_criterion.get(item.criterion_id, []) if finding.status == "pass"]
        if len(findings) < 2:
            continue
        conflict = conflict_checker(findings)
        if not conflict:
            continue
        target = findings[0]
        page_ids = []
        for finding in findings:
            page_ids.extend(packet_by_id.get(finding.packet_id, EvidencePacket(packet_id=finding.packet_id, criterion_id=finding.criterion_id, evidence_id=finding.evidence_id, specialist=finding.specialist, document_name="")).page_ids)
        additions.append(
            SpecialistFinding(
                packet_id=f"CROSS-{item.criterion_id}",
                criterion_id=item.criterion_id,
                evidence_id=target.evidence_id,
                specialist="cross_document_reconciliation",
                status="fail",
                check_results={"cross_document_match": "fail"},
                reason=conflict,
                page_ids=sorted(set(page_ids)),
            )
        )
    if additions:
        return SpecialistFindingSet(findings=[*specialist_findings.findings, *additions])
    return specialist_findings


def cross_document_conflict(findings: list[SpecialistFinding]) -> str | None:
    pan_values = sorted({
        normalize_fact_value(finding.facts.get("pan"))
        for finding in findings
        if finding.facts.get("pan")
        and finding.facts.get("pan_entity_role") == "bidder"
        and finding.facts.get("pan_confidence", "high") == "high"
    })
    pan_values = [value for value in pan_values if value]
    if len(pan_values) > 1:
        return f"Cross-document mismatch: PAN differs across evidence packets ({'; '.join(pan_values[:3])})."

    gstin_values = sorted({
        normalize_fact_value(finding.facts.get("gstin"))
        for finding in findings
        if finding.facts.get("gstin")
        and finding.facts.get("gstin_entity_role") == "bidder"
        and finding.facts.get("gstin_confidence", "high") == "high"
    })
    embedded_pans = sorted({value[2:12] for value in gstin_values if len(value) == 15})
    if len(embedded_pans) > 1:
        return f"Cross-document mismatch: GSTIN PAN differs across evidence packets ({'; '.join(embedded_pans[:3])})."
    if len(gstin_values) > 1 and not embedded_pans:
        return f"Cross-document mismatch: GSTIN differs across evidence packets ({'; '.join(gstin_values[:3])})."
    if pan_values and embedded_pans and pan_values[0] != embedded_pans[0]:
        return (
            "Cross-document mismatch: bidder PAN does not match the PAN embedded "
            f"in bidder GSTIN ({pan_values[0]} vs {embedded_pans[0]})."
        )

    for key in ["party_name"]:
        values = sorted({
            normalize_fact_value(finding.facts.get(key))
            for finding in findings
            if finding.facts.get(key)
            and finding.facts.get(f"{key}_entity_role") == "bidder"
        })
        values = [value for value in values if value]
        if len(values) > 1:
            return f"Cross-document mismatch: {key.replace('_', ' ')} differs across evidence packets ({'; '.join(values[:3])})."
    return None


def run_specialist_agent(
    packet: EvidencePacket,
    item: RequirementMatrixItem | None,
    evidence: EvidenceRequirement | None,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> SpecialistFinding:
    text = normalize_for_match(packet.text)
    if not text:
        return SpecialistFinding(
            packet_id=packet.packet_id,
            criterion_id=packet.criterion_id,
            evidence_id=packet.evidence_id,
            specialist=packet.specialist,
            status="missing",
            check_results={"presence": "missing"},
            facts=packet.extracted_facts,
            reason=f"No readable content found for {packet.document_name}.",
            page_ids=packet.page_ids,
        )

    evidence = evidence or EvidenceRequirement(evidence_id=packet.evidence_id, document_name=packet.document_name, document_type=packet.document_type)
    item = item or RequirementMatrixItem(criterion_id=packet.criterion_id, requirement="")
    rule_result = _hook(compatibility_hooks, "specialist_rule_result", specialist_rule_result)
    status, reason, quote = rule_result(packet.specialist, evidence, item, text)
    check_results = {"presence": "pass" if status in {"pass", "fail", "unclear"} else "missing"}
    if status == "pass":
        check_results["specialist_content"] = "pass"
    elif status == "fail":
        check_results["specialist_content"] = "fail"
    elif status == "unclear":
        check_results["specialist_content"] = "unclear"
    return SpecialistFinding(
        packet_id=packet.packet_id,
        criterion_id=packet.criterion_id,
        evidence_id=packet.evidence_id,
        specialist=packet.specialist,
        status=status,
        check_results=check_results,
        facts=packet.extracted_facts,
        reason=reason,
        quote=quote,
        page_ids=packet.page_ids,
    )


def specialist_rule_result(
    specialist: str,
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    text: str,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> tuple[Literal["pass", "fail", "missing", "unclear"], str, str | None]:
    name = f"{evidence.document_name} {evidence.document_type}".lower()
    if specialist == "udyam_mse":
        if any(marker in text for marker in ["udyam registration certificate", "udyam number", "enterprise name"]):
            return "pass", "Udyam/MSME specialist found registration evidence.", best_quote(text, ["udyam registration certificate", "udyam number"])
        return "missing", "Udyam/MSME specialist did not find Udyam registration evidence.", None
    if specialist == "financial_udin":
        requires_udin = "udin" in name or "ca certificate" in name or "financial_certificate" in evidence.document_type
        has_udin = bool(re.search(r"\budin\b", text)) or bool(re.search(r"\b\d{2}\d{6}[a-z0-9]{10}\b", text))
        has_financials = any(marker in text for marker in ["turnover", "net worth", "balance sheet", "profit and loss", "auditor", "chartered accountant", "ca certificate"])
        if has_udin and has_financials:
            return "pass", "Financial specialist found CA/UDIN or financial statement evidence.", best_quote(text, ["udin", "turnover", "balance sheet"])
        if requires_udin and has_financials and not has_udin:
            return "unclear", "Financial specialist found financial evidence but the required UDIN is not clear.", best_quote(text, ["turnover", "balance sheet", "chartered accountant"])
        if has_financials:
            return "unclear", "Financial specialist found financial evidence but UDIN/audit support is unclear.", best_quote(text, ["turnover", "balance sheet", "chartered accountant"])
        return "missing", "Financial specialist did not find financial/UDIN evidence.", None
    if specialist == "po_work_order":
        has_order = any(marker in text for marker in ["purchase order", "work order", "letter of intent", "loi", "award of work"])
        has_order_detail = any(marker in text for marker in ["order no", "po no", "work order no", "date", "supplier", "contractor", "buyer", "value", "amount"])
        if has_order and has_order_detail:
            return "pass", "PO/work-order specialist found award/order evidence.", best_quote(text, ["purchase order", "work order", "letter of intent"])
        if has_order:
            return "unclear", "PO/work-order specialist found order language but order number/date/supplier/value is unclear.", best_quote(text, ["purchase order", "work order", "letter of intent"])
        return "missing", "PO/work-order specialist did not find award/order evidence.", None
    if specialist == "completion_execution":
        has_execution = any(marker in text for marker in ["completion certificate", "work completion", "final bill", "tax invoice", "invoice", "service entry sheet", "executed value", "ra bill", "payment advice"])
        has_execution_detail = any(marker in text for marker in ["date", "value", "amount", "completed", "executed", "invoice no", "ses", "service entry"])
        if has_execution and has_execution_detail:
            return "pass", "Execution specialist found completion/invoice/SES evidence.", best_quote(text, ["completion certificate", "final bill", "service entry sheet", "executed value"])
        if has_execution:
            return "unclear", "Execution specialist found execution language but value/date linkage is unclear.", best_quote(text, ["completion certificate", "final bill", "service entry sheet", "executed value"])
        return "missing", "Execution specialist did not find completion/invoice/SES evidence.", None
    if specialist == "local_content":
        has_supplier_class = any(marker in text for marker in ["class-i", "class i", "class-ii", "class ii"])
        has_local_percent = bool(re.search(r"local content.{0,80}\d+(?:\.\d+)?\s*%", text)) or bool(re.search(r"\d+(?:\.\d+)?\s*%.{0,80}local content", text))
        if has_supplier_class or has_local_percent:
            return "pass", "Local-content specialist found MII/local supplier evidence.", best_quote(text, ["local content", "class-i", "class ii", "make in india"])
        if "make in india" in text or "public procurement preference" in text or "local content" in text:
            return "unclear", "Local-content specialist found MII language but no clear Class-I/Class-II or local-content percentage.", best_quote(text, ["local content", "make in india"])
        return "missing", "Local-content specialist did not find local supplier evidence.", None
    if specialist == "third_party_assurance":
        has_third_party = any(
            marker in text
            for marker in ("third party inspection", "tpia", "tpi agency", "bureau veritas", "independent inspection")
        )
        has_assurance_action = any(
            marker in text
            for marker in (
                "verified by us", "we have verified", "inspection report",
                "certificate of inspection", "inspected and verified", "certify that",
            )
        )
        if has_third_party and has_assurance_action:
            return "pass", "Independent third-party verification evidence is present.", best_quote(
                text,
                ["verified by us", "we have verified", "inspection report", "certificate of inspection"],
            )
        if has_third_party:
            return "unclear", "Third-party inspection is referenced, but completed independent verification is not proven.", best_quote(
                text, ["third party inspection", "tpia", "bureau veritas"]
            )
        return "missing", "Independent third-party verification evidence was not found.", None
    if specialist == "declaration":
        if "nil deviation" in name:
            if "nil deviation" in text:
                return "pass", "Declaration specialist found nil deviation declaration evidence.", best_quote(text, ["nil deviation"])
            return "missing", "Declaration specialist did not find nil deviation evidence.", None
        if any(term in name for term in ["blacklist", "holiday", "debar"]):
            if any(marker in text for marker in ["not blacklisted", "not debarred", "holiday list", "blacklisting", "debarred"]):
                return "pass", "Declaration specialist found blacklisting/holiday declaration evidence.", best_quote(text, ["not blacklisted", "not debarred", "holiday"])
            return "missing", "Declaration specialist did not find blacklisting/holiday evidence.", None
        identity_terms = _declaration_identity_terms(evidence.document_name)
        matched_terms = [term for term in identity_terms if term in text]
        if declaration_subject_matches(evidence.document_name, text):
            quote = best_quote(text, matched_terms)
            if quote and re.search(r"\bapplicable\b.{0,40}\bno\b", quote, flags=re.IGNORECASE):
                return "missing", f"The bidder checklist marks {evidence.document_name} as not enclosed.", quote
            return "pass", f"Declaration specialist matched {evidence.document_name}.", quote
        if any(marker in text for marker in ["declaration", "undertaking", "we declare", "we confirm", "integrity pact"]):
            return "unclear", f"Declaration pages were found, but none was conclusively matched to {evidence.document_name}.", best_quote(text, ["declaration", "undertaking", "integrity pact"])
        return "missing", "Declaration specialist did not find declaration evidence.", None
    if specialist == "emd_epbg":
        if "epbg" in name or "performance bank guarantee" in name:
            if any(marker in text for marker in ["epbg", "performance bank guarantee", "bank guarantee"]):
                return "pass", "EMD/ePBG specialist found performance bank guarantee evidence.", best_quote(text, ["epbg", "performance bank guarantee", "bank guarantee"])
            return "missing", "EMD/ePBG specialist did not find ePBG evidence.", None
        exemption_allowed = any(term in name for term in ["exemption", "udyam", "mse", "msme"])
        if exemption_allowed and any(marker in text for marker in ["udyam registration certificate", "udyam number"]):
            state, reason = emd_exemption_state(item, text)
            return ("pass" if state == "pass" else "unclear"), reason, best_quote(text, ["udyam registration certificate", "udyam number"])
        if emd_payment_present(text):
            return "pass", "EMD/ePBG specialist found EMD or exemption evidence.", best_quote(text, ["udyam registration certificate", "emd", "earnest money", "bid security"])
        return "missing", "EMD/ePBG specialist did not find EMD/exemption evidence.", None
    if specialist == "technical_compliance":
        if any(marker in text for marker in ["technical specification", "compliance", "accepted", "deviation", "datasheet", "boq"]):
            return "pass", "Technical specialist found technical compliance evidence.", best_quote(text, ["technical specification", "compliance", "datasheet"])
        return "unclear", "Technical specialist found readable content but no clear compliance marker.", best_quote(text, ["technical", "compliance"])
    marker_score = _hook(compatibility_hooks, "evidence_marker_score", evidence_marker_score)
    if marker_score(evidence, text) > 0:
        return "pass", "Generic specialist found evidence markers.", best_quote(text, [evidence.document_name.lower()])
    return "unclear", "Generic specialist could not determine this evidence conclusively.", best_quote(text, [evidence.document_name.lower()])


def specialist_type_for_evidence(entry: EvidenceMapEntry | EvidenceRequirement) -> str:
    text = f"{entry.document_name} {entry.document_type}".lower()
    if any(term in text for term in ["udyam", "msme", "mse", "startup"]):
        return "udyam_mse"
    if any(term in text for term in ["turnover", "net worth", "financial", "balance sheet", "ca certificate", "udin", "audited"]):
        return "financial_udin"
    if any(term in text for term in ["purchase order", "work order", "loi", "letter of intent"]):
        return "po_work_order"
    if any(term in text for term in ["completion", "invoice", "final bill", "execution", "ses"]):
        return "completion_execution"
    if any(term in text for term in ["local content", "class-i", "class ii", "make in india", "mii"]):
        return "local_content"
    if any(term in text for term in ["third party inspection", "third-party inspection", "tpia", "tpi", "bureau veritas"]):
        return "third_party_assurance"
    if any(term in text for term in ["emd", "epbg", "bid security", "bank guarantee"]):
        return "emd_epbg"
    if any(term in text for term in ["declaration", "undertaking", "blacklist", "holiday", "debar", "integrity", "nil deviation"]):
        return "declaration"
    if any(term in text for term in ["technical", "specification", "datasheet", "boq", "compliance"]):
        return "technical_compliance"
    return "generic_document"


def apply_specialist_findings_to_verification(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    findings: list[SpecialistFinding],
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> None:
    relevant = [finding for finding in findings if finding.criterion_id == entry.criterion_id and finding.evidence_id == entry.evidence_id]
    if not relevant:
        return
    verification.specialist_findings.extend(model_dump(finding) for finding in relevant)
    best_finding = _hook(compatibility_hooks, "best_specialist_finding", best_specialist_finding)
    apply_fact_checks = _hook(compatibility_hooks, "apply_specialist_fact_checks", apply_specialist_fact_checks)
    can_upgrade = _hook(compatibility_hooks, "specialist_pass_can_upgrade_verification", specialist_pass_can_upgrade_verification)
    best = best_finding(relevant)
    if best.status == "pass":
        verification.check_results["specialist_content"] = "pass"
        apply_fact_checks(verification, entry, best)
        identity_confirmed = specialist_confirms_document_identity(entry, best)
        if verification.status == "missing" and entry.matched_logical_document_ids and identity_confirmed:
            verification.check_results["presence"] = "pass"
            verification.reason = best.reason
            verification.quote = best.quote
            verification.page_ids = list(best.page_ids)
            verification.status = "unclear"
        if (
            verification.status == "missing"
            and entry.matched_logical_document_ids
            and can_upgrade(verification, entry)
        ):
            verification.check_results["presence"] = "pass"
            verification.status = "pass"
            verification.reason = best.reason
            verification.quote = best.quote
    elif best.status in {"fail", "missing"} and best.specialist == "cross_document_reconciliation":
        verification.status = best.status
        verification.reason = best.reason
        verification.quote = best.quote
        verification.check_results.update(best.check_results)
    verification.status = status_from_check_results(verification.status, verification.check_results)


def apply_specialist_fact_checks(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    finding: SpecialistFinding,
) -> None:
    check_names = {str(check).lower() for check in entry.checks} | {str(check).lower() for check in verification.check_results}
    if finding.facts.get("udin"):
        for check in {"udin", "udin_if_ca"} & check_names:
            verification.check_results[check] = "pass"
    if finding.facts.get("party_name") and "bidder_name_match" in check_names:
        verification.check_results["bidder_name_match"] = "pass"


def specialist_pass_can_upgrade_verification(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    *,
    strict_evidence_checks: set[str] | None = None,
) -> bool:
    check_results = {str(key).lower(): str(value).lower() for key, value in verification.check_results.items()}
    configured_checks = STRICT_EVIDENCE_CHECKS if strict_evidence_checks is None else strict_evidence_checks
    strict_checks = {str(check).lower() for check in entry.checks if str(check).lower() in configured_checks}
    strict_checks.update(key for key in check_results if key in configured_checks)
    for check in strict_checks:
        if check_results.get(check) in {"fail", "missing", "unclear"}:
            return False
    return not any(check_results.get(key) == "unclear" for key in ["signed", "stamped"])


def best_specialist_finding(findings: list[SpecialistFinding]) -> SpecialistFinding:
    rank = {"fail": 4, "pass": 3, "unclear": 2, "missing": 1}
    return max(findings, key=lambda finding: rank.get(finding.status, 0))


def verification_needs_recovery(verification: DocumentVerification) -> bool:
    """A local contradiction is not final until the wider bidder corpus is checked."""
    return verification.status in {"fail", "missing", "unclear"}


__all__ = [
    "STRICT_EVIDENCE_CHECKS",
    "specialist_orchestrator_agent",
    "cross_document_reconciliation_agent",
    "cross_document_conflict",
    "run_specialist_agent",
    "specialist_rule_result",
    "specialist_type_for_evidence",
    "apply_specialist_findings_to_verification",
    "apply_specialist_fact_checks",
    "specialist_pass_can_upgrade_verification",
    "best_specialist_finding",
    "verification_needs_recovery",
    "specialist_confirms_document_identity",
    "declaration_subject_matches",
    "annexure_labels",
]
