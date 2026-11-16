"""Deterministic requirement planning and evidence typing."""

from __future__ import annotations

import re

from procurement.domain.document_dictionary import procurement_dictionary_entries_for_text
from procurement.domain.schemas import (
    Criterion,
    EvidenceRequirement,
    RequirementMatrix,
    RequirementMatrixItem,
)
from procurement.domain.text import keyword_tokens, normalize_for_match
from procurement.evaluation.facts import canonical_evidence_type
from procurement.evaluation.normalization import is_post_award_criterion


def backfill_requirement_matrix(tender: TenderProfile, matrix: RequirementMatrix) -> RequirementMatrix:
    existing = {item.criterion_id: item for item in matrix.items}
    for criterion in tender.criteria:
        if criterion.criterion_id not in existing:
            matrix.items.append(matrix_item_from_criterion(criterion))
    for item in matrix.items:
        criterion = next((candidate for candidate in tender.criteria if candidate.criterion_id == item.criterion_id), None)
        if not criterion:
            continue
        item.requirement = item.requirement or criterion.requirement
        item.category = item.category or criterion.category
        item.stage = item.stage or criterion.stage
        item.mandatory = criterion.mandatory
        item.clarifiable = criterion.clarifiable
        item.failure_effect = item.failure_effect or criterion.failure_effect
        if not item.required_evidence:
            item.required_evidence = evidence_requirements_from_criterion(criterion)
        normalize_requirement_checks(criterion, item)
        if is_post_award_criterion(criterion):
            item.mandatory = False
            item.failure_effect = "no_direct_fail"
            for evidence in item.required_evidence:
                evidence.required = False
                evidence.missing_effect = "optional"
    return matrix


def normalize_requirement_checks(criterion: Criterion, item: RequirementMatrixItem) -> None:
    reviewed = (item.extensions or {}).get("reviewed_field") or {}
    atomic_text = " ".join(
        [
            str(reviewed.get("atomic_value") or ""),
            criterion.source_quote or "",
        ]
    ).lower()
    for evidence in item.required_evidence:
        checks = evidence.checks or infer_checks(criterion, evidence.document_name)
        artifact_text = " ".join(
            [
                evidence.document_name,
                evidence.document_type,
                evidence.acceptance_rule,
            ]
        ).lower()
        generic = any(
            marker in artifact_text
            for marker in ("supporting this tender requirement", "bidder evidence for")
        ) or evidence.document_type in {"", "unknown", "other"}
        evidence_text = f"{artifact_text} {atomic_text if generic else ''}"
        needs_letterhead = any(
            phrase in evidence_text for phrase in ["letterhead", "letter head", "company letter"]
        )
        if any(term in evidence_text for term in ["signed", "signature", "authorized signatory"]):
            checks.append("signed")
        if any(term in evidence_text for term in ["stamped", "stamp paper", "company seal"]):
            checks.append("stamped")
        if re.search(r"\budin\b", evidence_text):
            checks.append("udin")
        if "financial" in evidence_text and re.search(
            r"\b(?:last\s+)?(?:three|3|two|2|five|5)[ -]?(?:financial\s+)?years?\b",
            evidence_text,
        ):
            checks.append("financial_years")
        if not needs_letterhead and (
            evidence.document_type == "declaration" or "declaration" in evidence.document_name.lower()
        ):
            checks = [check for check in checks if check.lower() not in {"bidder_letterhead", "letterhead"}]
        evidence.checks = sorted(set(checks))


def matrix_item_from_criterion(criterion: Criterion) -> RequirementMatrixItem:
    return RequirementMatrixItem(
        criterion_id=criterion.criterion_id,
        requirement=criterion.requirement,
        category=criterion.category,
        stage=criterion.stage,
        mandatory=criterion.mandatory,
        clarifiable=criterion.clarifiable,
        failure_effect=criterion.failure_effect,
        pass_rule=criterion.pass_condition or "all_required_evidence_must_pass",
        source_quote=criterion.source_quote,
        criterion_kind=criterion.criterion_kind,
        evaluation_scope=criterion.evaluation_scope,
        materiality=criterion.materiality,
        applicability=dict(criterion.applicability or {}),
        decision_effect=criterion.decision_effect,
        semantic_key=criterion.semantic_key,
        threshold=dict(criterion.threshold or {}),
        evidence_groups=list(criterion.evidence_groups or []),
        depends_on=list(criterion.depends_on or []),
        blocks=list(criterion.blocks or []),
        source_references=list(criterion.source_references or []),
        required_evidence=evidence_requirements_from_criterion(criterion),
        extensions=dict(criterion.extensions or {}),
    )


def evidence_requirements_from_criterion(criterion: Criterion) -> list[EvidenceRequirement]:
    documents = criterion.required_documents or [criterion.requirement]
    requirements = []
    for index, document_name in enumerate(documents, start=1):
        requirements.append(
            EvidenceRequirement(
                evidence_id=f"{criterion.criterion_id}-E{index}",
                document_name=document_name,
                # The criterion wording is a search target, not proof of an
                # artifact type.  Typed evidence must come from the frozen
                # checklist contract; unknown evidence is searched
                # semantically across the full bidder corpus.
                document_type="unknown",
                required=criterion.mandatory,
                checks=infer_checks(criterion, document_name),
                acceptance_rule=criterion.pass_condition,
                missing_effect="reject" if criterion.mandatory else "manual_review",
            )
        )
    return requirements


def infer_document_type(name: str) -> str:
    dictionary_matches = procurement_dictionary_entries_for_text(name)
    if dictionary_matches:
        return str(dictionary_matches[0].get("evidence_type") or dictionary_matches[0].get("canonical_key") or "other")
    text = normalize_for_match(name)
    tokens = keyword_tokens(text)

    def matches(needle: str) -> bool:
        if len(needle) > 4:
            return needle in text
        return needle in tokens or bool(
            re.search(rf"(?<![a-z]){re.escape(needle)}(?=\d|[^a-z0-9]|$)", text)
        )

    checks = [
        ("emd_or_bid_security", ["emd", "bid security"]),
        ("experience_po_completion", ["purchase order", "po", "completion", "invoice", "execution", "tpi"]),
        ("turnover_networth_ca_certificate", ["turnover", "net worth", "balance sheet", "financial"]),
        ("mse_udyam_or_startup_exemption", ["udyam", "msme", "mse", "startup"]),
        ("mii_local_content", ["local content", "class-i", "class ii", "make in india"]),
        ("forms_annexures", ["nil deviation", "deviation declaration", "undertaking", "compliance declaration"]),
        ("integrity_blacklisting_declaration", ["blacklist", "holiday", "integrity", "debar"]),
        ("technical_proposal", ["technical", "compliance", "deviation", "specification", "vapt", "is audit", "security audit", "penetration test"]),
        ("quality_certificate", ["iso", "quality"]),
        ("calibration_certificate", ["calibration certificate", "calibration record", "calibrated on"]),
        ("oem_authorization", ["oem", "authorization", "manufacturer"]),
        ("poa_or_board_authorization", ["power of attorney", "poa", "board resolution", "board authorization", "board authorisation"]),
    ]
    for doc_type, needles in checks:
        if any(matches(needle) for needle in needles):
            return doc_type
    return "other"


def resolved_evidence_type(item: RequirementMatrixItem, evidence: EvidenceRequirement) -> str:
    del item
    return canonical_evidence_type(evidence.document_type or "unknown")


def infer_checks(criterion: Criterion, document_name: str) -> list[str]:
    text = f"{criterion.requirement} {criterion.pass_condition} {document_name}".lower()
    checks = ["presence"]
    if any(word in text for word in ["signed", "signature", "stamped", "seal", "declaration", "form", "undertaking", "pact"]):
        checks.extend(["signed", "stamped"])
    if any(word in text for word in ["date", "valid", "years", "last", "expiry", "bid validity"]):
        checks.append("date_valid")
    if any(word in text for word in ["lakh", "crore", "turnover", "value", "amount", "price", "financial"]):
        checks.append("value_threshold")
    if "financial" in text and re.search(
        r"\b(?:last\s+)?(?:three|3|two|2|five|5)[ -]?(?:financial\s+)?years?\b",
        text,
    ):
        checks.append("financial_years")
    if any(word in text for word in ["purchase order", "completion", "invoice", "experience", "similar"]):
        checks.extend(["item_similarity", "execution_proof"])
    checks.append("bidder_name_match")
    return sorted(set(checks))
