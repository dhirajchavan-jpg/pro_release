"""Policy, declaration, and statutory-compliance validators."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.schemas import CriterionValidatorResult, RequirementMatrixItem
from procurement.domain.text import normalize_for_match
from procurement.evaluation.facts import declared_local_content_percent, unfilled_local_content_template
from .validators_shared import has_udin_signal, validator_result


def emd_payment_present(text: str) -> bool:
    if not re.search(r"\b(?:emd|earnest money|bid security)\b", text, flags=re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:emd|earnest money|bid security)\b.{0,80}\b(?:not paid|not submitted|missing|waived|exempt)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:utr|transaction|receipt|bank guarantee)\b.{0,40}\b(?:no\.?|number|id|reference|dated)\b"
            r"|\b(?:no\.?|number|id|reference)\b.{0,40}\b(?:utr|transaction|receipt|bank guarantee)\b"
            r"|\b(?:emd|earnest money|bid security)\b.{0,40}\b(?:paid|deposited|remitted|submitted)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def emd_exemption_state(item: RequirementMatrixItem, text: str) -> tuple[str, str]:
    policy_id = str((item.extensions or {}).get("governing_policy_id") or "")
    has_udyam = bool(
        re.search(r"\budyam-[a-z]{2}-\d{2}-\d+\b", text, flags=re.IGNORECASE)
        or "udyam registration certificate" in text
        or "udyam number" in text
    )
    category = udyam_enterprise_category(text)
    startup = "startup" in text and "dpiit" in text and any(
        marker in text for marker in ("recognition", "recognised", "recognized", "certificate")
    )
    if not has_udyam and not startup and not any(
        marker in text for marker in ("msme", "micro and small", "emd exemption")
    ):
        return "none", ""
    if policy_id == "bhel_purchase_policy":
        if startup:
            return "pass", "DPIIT-recognized startup evidence supports the Purchase Policy EMD exception."
        if has_udyam and category in {"micro", "small"}:
            return "pass", f"Udyam evidence identifies the bidder as a {category} enterprise under the Purchase Policy EMD exception."
        return "unclear", "The Purchase Policy exception requires a verified Micro/Small Udyam category or DPIIT startup recognition; the current text does not establish that."
    if policy_id == "bhel_works_policy":
        return "unclear", "Udyam/MSME status alone is not an EMD exception under the Works Policy; a tender-authorized Clause 5.1.3 waiver and its approval must be verified."
    return "unclear", "The governing policy is unresolved, so the claimed EMD exemption cannot be validated."


def validate_emd_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    if emd_payment_present(text):
        return validator_result(
            item,
            "pass",
            "EMD/bid security or valid exemption evidence is present.",
            {"emd_or_exemption": "pass"},
            0.88,
            False,
            candidate,
        )
    exemption_state, exemption_reason = emd_exemption_state(item, text)
    if exemption_state in {"pass", "unclear"}:
        return validator_result(
            item,
            exemption_state,
            exemption_reason,
            {"emd_exemption": exemption_state},
            0.9 if exemption_state == "pass" else 0.75,
            False,
            candidate,
        )
    return validator_result(
        item,
        "missing",
        "EMD or valid exemption: missing.",
        {"emd_or_exemption": "missing"},
        0.78,
        False,
        candidate,
        missing_documents=["EMD/bid security evidence"],
    )

def udyam_enterprise_category(text: str) -> str | None:
    """Return the latest Udyam enterprise type when the certificate is in the bid."""
    matches = re.findall(
        r"20\d{2}\s*[-/]\s*\d{2}.{0,40}\b(micro|small|medium|large)\b",
        text,
        flags=re.IGNORECASE,
    )
    if matches:
        return matches[0].casefold()
    match = re.search(
        r"type of enterprise\s*[:\s]*\b(micro|small|medium|large)\b",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).casefold() if match else None

def validate_mse_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    requirement = normalize_for_match(f"{item.requirement} {item.source_quote or ''}")
    category = udyam_enterprise_category(text)
    has_udyam = bool(
        re.search(r"\budyam-[a-z]{2}-\d{2}-\d+\b", text, flags=re.IGNORECASE)
        or "udyam registration" in text
    )
    benefit_gate = any(
        marker in requirement
        for marker in (
            "purchase preference",
            "45 days",
            "mse benefits",
            "mse oem",
            "traders",
            "payment terms preference",
        )
    )
    if has_udyam and category in {"medium", "large"} and benefit_gate:
        return validator_result(
            item,
            "not_evaluated",
            f"Udyam classifies the bidder as a {category} enterprise; MSE purchase preference and 45-day terms do not apply.",
            {"mse_udyam": "pass", "mse_category": category, "mse_benefits": "not_applicable"},
            0.9,
            False,
            candidate,
        )
    if has_udyam or any(term in text for term in ["udyam number", "micro and small"]):
        reason = "MSE/Udyam evidence is present."
        if category:
            reason = f"Udyam evidence is present ({category} enterprise)."
        return validator_result(
            item,
            "pass",
            reason,
            {"mse_udyam": "pass", **({"mse_category": category} if category else {})},
            0.9,
            False,
            candidate,
        )
    if (
        "ca certificate" in requirement
        and "udin" in requirement
        and has_udin_signal(text)
        and any(
            term in text
            for term in ["chartered accountant", "ca certificate", "m.no", "m no"]
        )
    ):
        return validator_result(
            item,
            "pass",
            "CA/UDIN-backed MSE support evidence is present.",
            {"mse_support": "pass", "udin": "pass"},
            0.82,
            False,
            candidate,
        )
    return validator_result(
        item,
        "missing",
        "Required MSE/Udyam evidence was not found.",
        {"mse_udyam": "missing"},
        0.78,
        False,
        candidate,
        missing_documents=["MSE/Udyam evidence"],
    )

def validate_local_content_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    if unfilled_local_content_template(text):
        missing = [
            evidence.document_name
            for evidence in item.required_evidence
            if evidence.required
        ] or ["Completed local-content declaration"]
        return validator_result(
            item,
            "missing",
            "The submitted local-content declaration is an unfilled template.",
            {"local_content": "missing"},
            0.98,
            True,
            candidate,
            missing_documents=missing,
        )
    has_class = any(
        term in text for term in ["class-i", "class i", "class-ii", "class ii"]
    )
    declaration_signal = any(
        marker in text
        for marker in (
            "we hereby certify",
            "we certify",
            "hereby declare",
            "bidder declaration",
            "seller declaration",
        )
    )
    has_percent = declared_local_content_percent(text) is not None
    if has_percent:
        return validator_result(
            item,
            "pass",
            "Local-content/Class-I/Class-II evidence is present.",
            {"local_content": "pass"},
            0.88,
            False,
            candidate,
        )
    bidder_declaration = has_class and declaration_signal
    if bidder_declaration:
        return validator_result(
            item,
            "unclear",
            "MII/local-content document is present but class or percentage is unclear.",
            {"local_content": "unclear"},
            0.68,
            False,
            candidate,
        )
    missing = [
        evidence.document_name
        for evidence in item.required_evidence
        if evidence.required
    ] or ["Local-content evidence"]
    return validator_result(
        item,
        "missing",
        "Required local-content evidence was not found.",
        {"local_content": "missing"},
        0.75,
        False,
        candidate,
        missing_documents=missing,
    )

def validate_declaration_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    requirement = normalize_for_match(
        f"{item.criterion_id} {item.requirement} {item.source_quote or ''}"
    )
    checks: dict[str, str] = {}
    if "holiday" in requirement or "blacklist" in requirement or "debar" in requirement:
        checks["declaration_presence"] = "pass" if any(
            term in text
            for term in [
                "not blacklisted",
                "not been blacklisted",
                "not debarred",
                "not been debarred",
                "not banned",
                "holiday",
                "banning",
                "no previous transgression",
                "declares that no previous transgression",
            ]
        ) else "missing"
    elif "technical deviation" in requirement or "nil deviation" in requirement:
        checks["declaration_presence"] = "pass" if any(
            term in text for term in ["nil deviation", "no deviation", "technical deviation"]
        ) else "missing"
    elif "integrity pact" in requirement:
        checks["declaration_presence"] = "pass" if "integrity pact" in text else "missing"
    elif "social media" in requirement:
        checks["declaration_presence"] = "pass" if (
            "social media" in text or "business partner" in text
        ) else "missing"
    elif (
        "commercial" in requirement
        or "declaration" in requirement
        or "undertaking" in requirement
    ):
        checks["declaration_presence"] = "pass" if any(
            term in text
            for term in [
                "declaration",
                "undertaking",
                "we declare",
                "we confirm",
                "agreed",
                "accepted",
            ]
        ) else "missing"
    else:
        return validator_result(
            item,
            "unchanged",
            "No declaration validator applies.",
            {},
            0.0,
            False,
            candidate,
        )
    if all(value == "pass" for value in checks.values()):
        return validator_result(
            item,
            "pass",
            "Required declaration/undertaking evidence is present.",
            checks,
            0.84,
            False,
            candidate,
        )
    missing = [
        evidence.document_name
        for evidence in item.required_evidence
        if evidence.required
    ] or ["Required declaration/undertaking"]
    return validator_result(
        item,
        "missing",
        "Required declaration/undertaking evidence was not found.",
        checks,
        0.72,
        False,
        candidate,
        missing_documents=missing,
    )

def validate_third_party_assurance_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    has_third_party = any(
        term in text
        for term in (
            "third party inspection",
            "tpia",
            "tpi agency",
            "bureau veritas",
            "independent inspection",
        )
    )
    has_assurance_action = any(
        term in text
        for term in (
            "verified by us",
            "we have verified",
            "inspection report",
            "certificate of inspection",
            "inspected and verified",
            "certify that",
        )
    )
    if has_third_party and has_assurance_action:
        return validator_result(
            item,
            "pass",
            "Independent third-party verification evidence is present.",
            {"third_party_verification": "pass"},
            0.9,
            False,
            candidate,
        )
    if has_third_party:
        return validator_result(
            item,
            "unclear",
            "Third-party inspection is referenced, but completed independent verification is not proven.",
            {"third_party_verification": "unclear"},
            0.68,
            False,
            candidate,
        )
    return validator_result(
        item,
        "missing",
        "Independent third-party verification evidence was not found.",
        {"third_party_verification": "missing"},
        0.78,
        False,
        candidate,
        missing_documents=["Independent third-party verification evidence"],
    )

__all__ = [
    "udyam_enterprise_category",
    "validate_declaration_criterion",
    "validate_emd_criterion",
    "validate_local_content_criterion",
    "validate_mse_criterion",
    "validate_third_party_assurance_criterion",
]
