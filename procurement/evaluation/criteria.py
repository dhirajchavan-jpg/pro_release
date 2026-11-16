"""Deterministic interpretation and routing of tender evaluation criteria."""

from __future__ import annotations

import os
import re

from procurement.domain.schemas import EvidenceRequirement, RequirementMatrixItem
from procurement.domain.text import keyword_tokens, normalize_for_match, unique_items
from procurement.evaluation.facts import canonical_evidence_type, exact_product_identity_anchors


def criterion_retrieval_policy(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
) -> dict[str, int | str]:
    """Choose bounded retrieval breadth from the evidence actually required."""
    text = normalize_for_match(
        " ".join(
            [
                criterion_atomic_requirement_text(item),
                item.pass_rule,
                evidence.document_name,
                evidence.document_type,
                " ".join(evidence.checks),
                evidence.acceptance_rule,
            ]
        )
    )
    objective_markers = (
        " pan ",
        " gstin ",
        " cin ",
        " udyam ",
        "certificate number",
        "registration number",
        "signature",
        "stamp",
        "seal",
        "publish date",
        "emd amount",
    )
    broad_markers = (
        "experience",
        "similar work",
        "past project",
        "implementation",
        "maintenance",
        "technical capability",
        "turnover",
        "net worth",
        "three financial years",
        "multiple",
        "combined",
        "all use cases",
        "infrastructure",
        "methodology",
    )
    if any(marker in f" {text} " for marker in objective_markers):
        return {
            "profile": "objective",
            "documents": max(1, int(os.getenv("EVIDENCE_OBJECTIVE_MAX_DOCUMENTS", "3"))),
            "semantic_pool": max(1, int(os.getenv("EVIDENCE_OBJECTIVE_SEMANTIC_POOL", "24"))),
            "rerank": max(1, int(os.getenv("EVIDENCE_OBJECTIVE_RERANK_CANDIDATES", "8"))),
            "final_pages": max(1, int(os.getenv("EVIDENCE_OBJECTIVE_FINAL_PAGES", "4"))),
        }
    # Each evidence requirement is routed independently.  A criterion listing
    # several distinct documents does not make every document a broad search.
    multi_document = any(marker in text for marker in broad_markers)
    if multi_document:
        return {
            "profile": "broad",
            "documents": max(1, int(os.getenv("EVIDENCE_BROAD_MAX_DOCUMENTS", "8"))),
            "semantic_pool": max(1, int(os.getenv("EVIDENCE_BROAD_SEMANTIC_POOL", "128"))),
            "rerank": max(1, int(os.getenv("EVIDENCE_BROAD_RERANK_CANDIDATES", "32"))),
            "final_pages": max(1, int(os.getenv("EVIDENCE_BROAD_FINAL_PAGES", "16"))),
        }
    return {
        "profile": "standard",
        "documents": max(1, int(os.getenv("EVIDENCE_STANDARD_MAX_DOCUMENTS", "4"))),
        "semantic_pool": max(1, int(os.getenv("EVIDENCE_STANDARD_SEMANTIC_POOL", "64"))),
        "rerank": max(1, int(os.getenv("EVIDENCE_STANDARD_RERANK_CANDIDATES", "16"))),
        "final_pages": max(1, int(os.getenv("EVIDENCE_STANDARD_FINAL_PAGES", "6"))),
    }


def criterion_signal_terms(item: RequirementMatrixItem) -> set[str]:
    text = " ".join(
        [
            item.requirement,
            item.pass_rule,
            item.source_quote or "",
            str((item.applicability or {}).get("rule") or ""),
            " ".join(str(value) for value in (item.applicability or {}).get("conditions") or []),
            " ".join(evidence.document_name for evidence in item.required_evidence),
            " ".join(evidence.document_type for evidence in item.required_evidence),
        ]
    )
    generic = {
        "bidder", "vendor", "submit", "submitted", "document", "documents", "required",
        "shall", "must", "copy", "proof", "certificate", "declaration", "valid",
        "criteria", "criterion", "tender", "rfp", "bid",
    }
    return {token for token in keyword_tokens(text) if token not in generic}


def criterion_atomic_requirement_text(item: RequirementMatrixItem) -> str:
    reviewed = (item.extensions or {}).get("reviewed_field")
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    atomic_value = str(reviewed.get("atomic_value") or "").strip()
    if atomic_value:
        return normalize_for_match(atomic_value)
    requirement = str(item.requirement or "").split(". Conditions:", 1)[0]
    return normalize_for_match(
        f"{requirement} "
        f"{' '.join(evidence.document_name for evidence in item.required_evidence)}"
    )


def criterion_response_mode(item: RequirementMatrixItem) -> str:
    """Return the bidder response shape required by the tender wording."""
    text = normalize_for_match(
        f"{item.requirement} {item.pass_rule} {item.source_quote or ''}"
    )
    artifact_nouns = (
        "certificate", "document", "drawing", "format", "list", "schedule",
        "report", "copy", "details", "datasheet", "catalogue", "catalog",
    )
    if re.search(r"\b(?:submit|provide|furnish|enclose|attach|upload)\b", text) and any(
        noun in text for noun in artifact_nouns
    ):
        return "artifact"
    if re.search(r"\b(?:specify|state|mention|indicate|quote|give)\b", text):
        return "value"
    if re.search(r"\b(?:confirm|accept|agree|comply|compliance)\b", text):
        return "confirmation"
    return "evidence"


def criterion_is_conditionally_worded(item: RequirementMatrixItem) -> bool:
    """Recognize bidder-specific conditions even when extraction missed the flag."""
    state = str((item.applicability or {}).get("state") or "applicable")
    if state in {"conditional", "not_applicable"}:
        return state == "conditional"
    text = normalize_for_match(
        f"{item.requirement} {item.source_quote or ''} "
        f"{(item.applicability or {}).get('rule') or ''}"
    )
    return bool(
        re.search(r"\b(?:if|when|where|unless|only if|in case of|applicable to|applies to)\b", text)
        or re.search(
            r"\b(?:foreign vendors?|agents? quoting|dealers?|resellers?|mse bidders?|"
            r"startup bidders?|bidders? from (?:a )?country)\b",
            text,
        )
    )


def criterion_has_similar_work_semantics(item: RequirementMatrixItem) -> bool:
    """Require criterion semantics, never an inferred evidence label, for work-history validation."""
    reviewed = (item.extensions or {}).get("reviewed_field")
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    atomic_text = criterion_atomic_requirement_text(item)
    # A point-wise PQR pack may mention purchase orders as one of several
    # proofs. That pack is not itself a similar-work count criterion.
    if any(
        marker in atomic_text
        for marker in (
            "pre-qualification",
            "pre qualification",
            "annexure-pqr",
            "annexure pqr",
            "point-wise",
            "point wise",
        )
    ):
        return False
    if str(reviewed.get("field_key") or "").casefold() in {
        "minimum_industry_experience",
        "min_industry_experience_years",
        "similar_project_qualification",
        "similar_past_projects_count",
        "similar_works_milestones",
    }:
        return True
    return any(
        marker in atomic_text
        for marker in (
            "similar work",
            "similar project",
            "past work",
            "past experience",
            "prior experience",
            "industry experience",
            "purchase order",
            "work order",
            "completion certificate",
            "executed contract",
        )
    )


def criterion_validator_kinds(item: RequirementMatrixItem) -> list[str]:
    typed_evidence = [
        evidence
        for evidence in item.required_evidence
        if canonical_evidence_type(evidence.document_type) not in {"unknown", "other"}
    ]
    if typed_evidence:
        core_text = normalize_for_match(
            f"{item.pass_rule} "
            f"{' '.join(evidence.document_name for evidence in typed_evidence)} "
            f"{' '.join(' '.join(evidence.checks) for evidence in typed_evidence)}"
        )
    else:
        core_text = normalize_for_match(
            f"{item.requirement} {item.pass_rule} {item.source_quote or ''} "
            f"{' '.join(evidence.document_name for evidence in item.required_evidence)} "
            f"{' '.join(' '.join(evidence.checks) for evidence in item.required_evidence)}"
        )
    text = normalize_for_match(
        f"{core_text} {' '.join(evidence.document_type for evidence in item.required_evidence)}"
    )
    tokens = keyword_tokens(text)
    kinds: list[str] = []
    if "turnover" in core_text or "average annual" in core_text or ("value_threshold" in core_text and "udin support" not in core_text):
        kinds.append("turnover")
    if re.search(r"\budin\b", text) or any(term in text for term in ["chartered accountant", "ca certificate", "audited financial"]):
        kinds.append("udin")
    if "net worth" in core_text or "networth" in core_text:
        kinds.append("net_worth")
    identifier_text = criterion_atomic_requirement_text(item)
    identifier_tokens = keyword_tokens(identifier_text)
    reviewed = (item.extensions or {}).get("reviewed_field")
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    reviewed_field_key = str(reviewed.get("field_key") or "").casefold()
    is_identifier_field = reviewed_field_key in {
        "statutory_registrations",
        "statutory_registrations_required",
    }
    is_direct_identifier_requirement = bool(
        re.match(r"^(?:pan|gst|gstin|cin)\b", identifier_text)
        or any(
            phrase in identifier_text
            for phrase in (
                "pan card",
                "permanent account number",
                "gst registration",
                "company identification number",
                "statutory registration",
            )
        )
    )
    if (
        identifier_tokens.intersection({"pan", "gst", "gstin", "cin"})
        and (is_identifier_field or is_direct_identifier_requirement)
    ):
        kinds.append("statutory_identifier")
    if any(term in text for term in ["emd", "earnest money", "bid security"]):
        kinds.append("emd")
    if any(term in text for term in ["udyam", "msme", "mse", "micro and small"]):
        kinds.append("mse")
    if any(term in text for term in ["local content", "class-i", "class i", "class-ii", "class ii", "make in india", "mii", "ppp-mii"]):
        kinds.append("local_content")
    if criterion_has_similar_work_semantics(item):
        kinds.append("similar_work")
    if any(term in text for term in ["third party inspection", "third-party inspection", "tpia", "tpi", "bureau veritas"]):
        kinds.append("third_party_assurance")
    if any(term in text for term in ["holiday", "blacklist", "debar", "nil deviation", "technical deviation", "integrity pact", "social media", "commercial declaration", "declaration", "undertaking"]):
        kinds.append("declaration")
    reviewed_field_key = str(reviewed.get("field_key") or "").casefold()
    if (
        reviewed_field_key == "solution_capabilities"
        and len(exact_product_identity_anchors(criterion_atomic_requirement_text(item))) >= 2
    ):
        kinds.append("product_identity")
    return unique_items(kinds)


def criterion_allows_alternative_route(item: RequirementMatrixItem) -> bool:
    text = normalize_for_match(
        f"{item.requirement} {item.pass_rule} {item.source_quote or ''} "
        f"{' '.join(evidence.document_name for evidence in item.required_evidence)}"
    )
    emd_routes = sum(
        any(term in evidence.document_name.lower() for term in [
            "bank guarantee", "demand draft", "pay order", "bid security declaration",
            "udyam", "mse", "msme",
        ])
        for evidence in item.required_evidence
    )
    return (
        criterion_is_prohibition(item)
        or
        " or " in f" {text} "
        or "exemption" in text
        or "either" in text
        or (any(term in text for term in ["emd", "earnest money", "bid security"]) and emd_routes >= 2)
    )


def criterion_is_prohibition(item: RequirementMatrixItem) -> bool:
    text = normalize_for_match(
        f"{item.requirement} {item.pass_rule} {item.source_quote or ''}"
    )
    subject = any(term in text for term in ("joint venture", "jv", "consortium"))
    prohibited = any(
        term in text
        for term in (
            "false",
            "not allowed",
            "not permitted",
            "prohibited",
            "single entity",
            "single bidder",
        )
    )
    return subject and prohibited


def normalize_prohibition_criterion(item: RequirementMatrixItem) -> None:
    if not criterion_is_prohibition(item):
        return
    item.requirement = (
        "JV / consortium participation is prohibited; the bid must be submitted "
        "by one eligible legal entity."
    )
    item.pass_rule = (
        "pass_when_bidder_is_one_eligible_legal_entity_and_no_jv_or_consortium_"
        "participation_is_present"
    )
    generic = not item.required_evidence or all(
        "supporting this tender requirement" in evidence.document_name.lower()
        for evidence in item.required_evidence
    )
    if generic:
        item.required_evidence = [
            EvidenceRequirement(
                evidence_id=f"{item.criterion_id}-E1",
                document_name="Bidder legal identity and participation structure",
                document_type="gst_pan_cin_statutory",
                required=True,
                checks=["single_legal_entity", "no_jv_or_consortium_participation"],
                acceptance_rule=(
                    "The submitted bid identifies one eligible legal entity and "
                    "contains no JV or consortium participation."
                ),
                missing_effect="manual_review",
            )
        ]


def criterion_kind_signal_score(kinds: list[str], text: str) -> int:
    score = 0
    tokens = keyword_tokens(text)
    if "turnover" in kinds and any(term in text for term in ["turnover", "balance sheet", "profit and loss", "annual report"]):
        score += 5
    if "udin" in kinds and ("udin" in text or "chartered accountant" in text or "ca certificate" in text):
        score += 5
    if "net_worth" in kinds and ("net worth" in text or "networth" in text):
        score += 5
    if "statutory_identifier" in kinds and (
        tokens.intersection({"pan", "gstin", "cin"}) or "gst registration" in text
    ):
        score += 5
    if "emd" in kinds and any(term in text for term in ["emd", "earnest money", "bid security", "utr", "bank guarantee"]):
        score += 5
    if "mse" in kinds and any(term in text for term in ["udyam", "msme", "micro and small", "mse"]):
        score += 5
    if "local_content" in kinds and any(term in text for term in ["local content", "class-i", "class i", "class-ii", "class ii", "make in india"]):
        score += 5
    if "similar_work" in kinds and any(term in text for term in ["purchase order", "work order", "completion certificate", "tax invoice", "executed value"]):
        score += 5
    if "third_party_assurance" in kinds and any(term in text for term in ["tpia", "third party inspection", "bureau veritas", "inspection report", "verified by us"]):
        score += 5
    if "declaration" in kinds and any(term in text for term in ["declaration", "undertaking", "integrity pact", "nil deviation", "holiday", "social media", "accepted", "agreed"]):
        score += 5
    return score


__all__ = [
    "criterion_allows_alternative_route",
    "criterion_atomic_requirement_text",
    "criterion_has_similar_work_semantics",
    "criterion_is_conditionally_worded",
    "criterion_is_prohibition",
    "criterion_kind_signal_score",
    "criterion_response_mode",
    "criterion_signal_terms",
    "criterion_validator_kinds",
    "normalize_prohibition_criterion",
]
