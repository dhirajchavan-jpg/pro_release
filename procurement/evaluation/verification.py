"""Deterministic document verification for bidder evidence."""

from __future__ import annotations

import re
from typing import Literal

from procurement.domain.schemas import (
    DocumentVerification,
    EvidenceMapEntry,
    RequirementMatrixItem,
)
from procurement.domain.text import humanize_check_name, normalize_for_match
from procurement.evaluation.criteria import criterion_atomic_requirement_text
from procurement.evaluation.evidence import IdentifierType
from procurement.evaluation.evidence import EvidenceLedger as EntityEvidenceLedger
from procurement.evaluation.facts import strict_identifier_artifact_metadata
from procurement.evaluation.planning import infer_document_type
from procurement.evaluation.validators import (
    financial_year_signal_count,
    has_udin_signal,
    identifier_artifact_required,
)
from procurement.understanding.classification import tender_source_trap


def apply_deterministic_verification(
    verification: DocumentVerification,
    item: RequirementMatrixItem,
    entry: EvidenceMapEntry,
    snippets: list[str],
    *,
    identifier_ledger: EntityEvidenceLedger | None = None,
    selected_page_ids: list[str] | None = None,
) -> None:
    raw_text = "\n".join(snippets)
    text = normalize_for_match(raw_text)
    visually_judged = any(
        check.get("mode") in {"document_vision", "gemini_document_vision"}
        and check.get("status") == "inspected"
        for check in verification.visual_checks
    )
    if not text or tender_source_trap(text):
        if visually_judged:
            return
        verification.status = "missing"
        verification.check_results["presence"] = "missing"
        verification.reason = f"Required document missing: {entry.document_name}"
        verification.quote = None
        return

    doc_type = entry.document_type or infer_document_type(entry.document_name)
    result = deterministic_document_result(doc_type, entry.document_name, item.requirement, text)
    if result:
        status, reason, quote = result
        if status == "fail" or (status == "missing" and not (visually_judged and verification.status == "pass")):
            verification.status = status
            verification.reason = reason
            verification.quote = quote
        if status == "pass" and verification.status == "pass":
            for check in entry.checks or ["presence"]:
                verification.check_results[check] = "pass"
        elif status == "fail":
            verification.check_results["presence"] = "pass"
            verification.check_results["substantive_match"] = "fail"
        elif status == "missing" and not (visually_judged and verification.status == "pass"):
            verification.check_results["presence"] = status

    check_names = {check.lower() for check in entry.checks} | {check.lower() for check in verification.check_results}
    if "participation_structure" in check_names:
        route_status = bidder_participation_route_status(item, text)
        if route_status:
            status, reason = route_status
            verification.check_results["participation_structure"] = status
            if status != "pass" and verification.status in {"pass", "unclear"}:
                verification.status = "unclear"
                verification.reason = reason
    if (
        "single_legal_entity" in check_names
        and verification.status == "pass"
        and re.search(r"\bm\s*/?\s*s\.?\s*[_\-.]{3,}", raw_text, re.IGNORECASE)
    ):
        verification.status = "unclear"
        verification.check_results["single_legal_entity"] = "unclear"
        verification.reason = "The cited form contains a blank bidder-name placeholder and does not prove one legal entity."
    if "udin" in check_names or "udin_if_ca" in check_names:
        verification.check_results["udin"] = "pass" if has_udin_signal(text) else "fail"
    if "financial_years" in check_names:
        verification.check_results["financial_years"] = "pass" if financial_year_signal_count(text) >= 3 else "fail"
    financial_failures = {
        check
        for check in {"udin", "udin_if_ca", "financial_years"}
        if verification.check_results.get(check) == "fail"
    }
    if financial_failures:
        verification.status = "fail"
        failed = [
            humanize_check_name(check)
            for check, value in verification.check_results.items()
            if value == "fail" and check in financial_failures
        ]
        verification.reason = f"{entry.document_name}: failed required check(s): {', '.join(failed)}."

    if not visually_judged and any("sign" in check.lower() or "stamp" in check.lower() or "seal" in check.lower() for check in entry.checks):
        signal = signature_stamp_signal(text)
        if signal == "strong":
            verification.check_results["signed"] = "pass"
            verification.check_results["stamped"] = "pass"
        elif signal in {"placeholder", "absent"} and verification.status == "pass":
            verification.status = "unclear"
            verification.reason = "Document is present but signature/stamp could not be verified from extracted text."
            verification.check_results["signed"] = "unclear"
            verification.check_results["stamped"] = "unclear"
    enforce_identifier_artifact_requirement(
        verification,
        item,
        entry,
        snippets,
        visually_judged,
        identifier_ledger,
        selected_page_ids or [],
    )
    verification.status = status_from_check_results(verification.status, verification.check_results)


def enforce_identifier_artifact_requirement(
    verification: DocumentVerification,
    item: RequirementMatrixItem,
    entry: EvidenceMapEntry,
    snippets: list[str],
    visually_judged: bool,
    identifier_ledger: EntityEvidenceLedger | None,
    selected_page_ids: list[str],
) -> None:
    if verification.status != "pass":
        return
    evidence_text = normalize_for_match(f"{entry.document_name} {' '.join(entry.checks)}")
    identifier_types = [
        identifier_type
        for identifier_type, markers in (
            (IdentifierType.PAN, ("pan", "permanent account number")),
            (IdentifierType.GSTIN, ("gst", "gstin")),
        )
        if any(marker in evidence_text for marker in markers)
        and identifier_artifact_required(item, identifier_type)
    ]
    for identifier_type in identifier_types:
        if identifier_ledger is None:
            artifact_found = any(
                strict_identifier_artifact_metadata(identifier_type, snippet, "unknown")[0]
                for snippet in snippets
            )
        else:
            accepted_labels = {
                "udyam_certificate",
                "pan_card" if identifier_type is IdentifierType.PAN else "gst_registration",
            }
            selected = set(selected_page_ids)
            artifact_found = any(
                observation.identifier_type is identifier_type
                and bool(observation.artifact_labels.intersection(accepted_labels))
                and observation.provenance.page_id in selected
                for observation in identifier_ledger.observations
            )
        if not artifact_found and visually_judged:
            artifact_found = bool(
                strict_identifier_artifact_metadata(
                    identifier_type,
                    f"{verification.reason} {verification.quote or ''}",
                    "unknown",
                )[0]
            )
        if artifact_found:
            continue
        key = identifier_type.value
        verification.status = "missing"
        verification.check_results[f"{key}_artifact"] = "missing"
        verification.reason = (
            f"The bidder's {key.upper()} value is visible, but the required "
            f"{entry.document_name} is not present as a submitted registration artifact."
        )
        verification.quote = None
        verification.page_ids = []


def bidder_participation_route_status(
    item: RequirementMatrixItem,
    text: str,
) -> tuple[Literal["pass", "unclear"], str] | None:
    """Require proof of an allowed route; legal identity alone is insufficient."""
    requirement = criterion_atomic_requirement_text(item)
    requires_local_route = any(
        marker in requirement
        for marker in (
            "class 1", "class i", "class-1", "class-i",
            "class 2", "class ii", "class-2", "class-ii",
            "local supplier", "mse oem", "mse manufacturer",
        )
    )
    if not requires_local_route:
        return None

    class_claim = bool(
        re.search(
            r"\b(?:we|bidder|seller|supplier|enterprise)\b[^#.;]{0,100}"
            r"\b(?:are|is|certif(?:y|ies)|declar(?:e|es))\b[^#.;]{0,100}"
            r"\bclass[- ]?(?:i{1,2}|1|2)\s+local\s+supplier\b",
            text,
        )
        or re.search(
            r"\bclass[- ]?(?:i{1,2}|1|2)\s+local\s+supplier\b.{0,100}"
            r"\b(?:certificate|declaration)\b",
            text,
        )
    )
    mse_registration = bool(re.search(r"\budyam-[a-z]{2}-\d{2}-\d{7}\b", text))
    offered_product_manufacturer_claim = bool(
        re.search(
            r"\b(?:we|bidder|seller|enterprise)\b[^#.;]{0,100}"
            r"\b(?:are|is|certif(?:y|ies)|declar(?:e|es))\b[^#.;]{0,100}"
            r"\b(?:manufacturer|oem)\b[^#.;]{0,100}"
            r"\b(?:offered product|offered item|tendered product|tendered item)\b",
            text,
        )
    )
    if class_claim or (mse_registration and offered_product_manufacturer_claim):
        return "pass", "An explicit permitted local-supplier or MSE manufacturer/OEM route is evidenced."
    return (
        "unclear",
        "The bidder's legal identity is established, but the submitted evidence does not "
        "conclusively prove Class-I/Class-II local-supplier status or MSE manufacturer/OEM "
        "eligibility for the offered product.",
    )


def deterministic_document_result(
    document_type: str,
    document_name: str,
    requirement: str,
    text: str,
) -> tuple[Literal["pass", "fail", "missing", "unclear"], str, str | None] | None:
    name = document_name.lower()
    wanted = f"{name} {document_type}".lower()
    if document_type == "mse_udyam_or_startup_exemption" or "udyam" in wanted or "msme" in wanted or "mse" in wanted:
        if any(marker in text for marker in ["udyam registration certificate", "udyam number", "enterprise name:"]):
            return "pass", "Valid Udyam/MSME evidence is present.", best_quote(text, ["udyam registration certificate", "udyam number"])
        return "missing", f"Required document missing: {document_name}", None
    if document_type == "emd_or_bid_security" or "emd" in wanted or "bid security" in wanted:
        if any(marker in text for marker in ["udyam registration certificate", "udyam number"]):
            return "pass", "Valid MSE/Udyam exemption evidence is present for EMD.", best_quote(text, ["udyam registration certificate", "udyam number"])
        if any(marker in text for marker in ["emd", "earnest money", "bid security"]) and any(
            marker in text for marker in ["receipt", "utr", "transaction", "payment", "bank guarantee", "bg no"]
        ):
            return "pass", "EMD/bid security payment evidence is present.", best_quote(text, ["emd", "earnest money", "bid security"])
        return "missing", f"Required document missing: {document_name}", None
    if document_type == "experience_po_completion":
        if "purchase order" in name or name == "po":
            if "purchase order" in text or ("po date" in text and "supplier" in text):
                return "pass", "Purchase order evidence is present.", best_quote(text, ["purchase order", "po date"])
            return "missing", f"Required document missing: {document_name}", None
        if any(term in name for term in ["completion", "execution", "invoice", "receipt"]):
            if "completion certificate" in text or "tax invoice" in text or "executed value" in text:
                return "pass", "Execution proof evidence is present.", best_quote(text, ["completion certificate", "tax invoice", "executed value"])
            return "missing", f"Required document missing: {document_name}", None
        if any(marker in text for marker in ["purchase order", "completion certificate", "tax invoice", "executed value"]):
            return "pass", "Experience evidence is present.", best_quote(text, ["purchase order", "completion certificate", "tax invoice"])
    if document_type == "oem_authorization" or "oem authorization" in wanted:
        if not any(marker in text for marker in ["oem authorization", "authorization letter", "authorised bidder", "authorized bidder"]):
            return "missing", f"Required document missing: {document_name}", None
        missing_models = [model for model in model_tokens(requirement) if model.lower() not in text]
        if missing_models:
            return "fail", f"OEM authorization is present but does not match required model(s): {', '.join(missing_models)}.", best_quote(text, ["authorized product", "authorised product", "model"])
        if any(phrase in text for phrase in ["not for model", "not for the required model", "different brand"]):
            return "fail", "OEM authorization is present but explicitly excludes the required model.", best_quote(text, ["not for model", "different brand"])
        return "pass", "OEM authorization evidence is present for the required model.", best_quote(text, ["oem authorization", "authorization letter"])
    if document_type == "integrity_blacklisting_declaration" or any(term in wanted for term in ["blacklist", "holiday", "integrity"]):
        if any(marker in text for marker in ["not blacklisted", "not been blacklisted", "not debarred", "not been debarred", "holiday list", "integrity pact"]):
            return "pass", "Blacklisting/holiday/integrity declaration is present.", best_quote(text, ["not blacklisted", "not been blacklisted", "not debarred", "not been debarred", "integrity pact"])
        return "missing", f"Required document missing: {document_name}", None
    if document_type == "mii_local_content" or "local content" in wanted:
        if any(marker in text for marker in ["local content", "class-i", "class ii", "make in india"]):
            return "pass", "Local content declaration is present.", best_quote(text, ["local content", "class-i", "class ii"])
        return "missing", f"Required document missing: {document_name}", None
    if document_type == "quality_certificate" or "iso" in wanted:
        if "iso" in text and "certificate" in text:
            return "pass", "Quality certificate evidence is present.", best_quote(text, ["iso", "certificate"])
        return "missing", f"Required document missing: {document_name}", None
    if "declaration" in wanted or "undertaking" in wanted or "nil deviation" in wanted or "deviation" in wanted:
        if any(marker in text for marker in ["declaration", "undertaking", "we declare", "we confirm", "nil deviation"]):
            return "pass", "Declaration/undertaking is present.", best_quote(text, ["declaration", "undertaking", "nil deviation"])
        return "missing", f"Required document missing: {document_name}", None
    return None


def model_tokens(text: str) -> list[str]:
    return sorted(set(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d+[A-Z0-9-]*\b", text)))


def best_quote(text: str, markers: list[str]) -> str | None:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for marker in markers:
        for sentence in sentences:
            if marker in sentence:
                return sentence.strip()[:240]
    return sentences[0].strip()[:240] if sentences else None


def signature_stamp_signal(text: str) -> Literal["strong", "placeholder", "absent"]:
    normalized = re.sub(r"[^a-z0-9]+", " ", normalize_for_match(text))
    placeholder_patterns = [
        "stamp signature of the bidder",
        "seal signature of the bidder",
        "with company seal signature",
        "authorized signatory with company seal signature",
    ]
    strong_patterns = [
        "digitally signed",
        "signed by",
        "seal affixed",
        "company stamp seal affixed",
        "signed and stamped",
        "signature affixed",
    ]
    if any(pattern in normalized for pattern in strong_patterns):
        return "strong"
    if any(pattern in normalized for pattern in placeholder_patterns):
        return "placeholder"
    return "absent"


def status_from_check_results(current: str, checks: dict[str, str]) -> Literal["pass", "fail", "missing", "unclear"]:
    values = {str(value).lower() for value in checks.values()}
    if "missing" in values:
        return "missing"
    if "fail" in values:
        return "fail"
    if "unclear" in values:
        return "unclear"
    if values and values <= {"pass"}:
        return "pass"
    if current in {"pass", "fail", "missing", "unclear"}:
        return current  # type: ignore[return-value]
    return "unclear"
