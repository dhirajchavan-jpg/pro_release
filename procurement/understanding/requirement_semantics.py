"""Deterministic bidder-requirement applicability and materiality semantics."""

from __future__ import annotations

import hashlib
import re
from typing import Any


_SHARED_CALL_BUDGET_NOTE = "Focused-understanding call budget was exhausted."
_CONDITIONAL_POLICY_FIELDS = {"mse_startup_policy", "make_in_india_policy"}
_COMMERCIAL_CONTEXT_FIELDS = {"purchase_preference_and_allocation"}


def _atomic_requirement_values(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        return [
            f"{key}: {item}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _requirement_semantics(
    field_key: str,
    text: str,
    *,
    field_status: str = "found",
    conditions: list[str] | None = None,
    exceptions: list[str] | None = None,
    source_text: str = "",
    source_grounded: bool = True,
) -> dict[str, Any]:
    context = "; ".join([text, *(conditions or []), *(exceptions or [])])
    text_normalized = " ".join(text.casefold().split())
    normalized = " ".join(context.casefold().split())
    source_normalized = " ".join(source_text.casefold().split())
    semantic_normalized = " ".join(part for part in (text_normalized, source_normalized) if part)
    lifecycle_normalized = " ".join(part for part in (normalized, source_normalized) if part)
    clauses = [clause.strip() for clause in text_normalized.split(";") if clause.strip()]
    negative_markers = (
        "not applicable",
        "not required",
        "does not apply",
        "reserved for mse: no",
        "emd: no",
        "emd amount: 0",
        "tpi required: na",
        "third party inspection required: na",
        "integrity pact: na",
    )
    # A composite policy can contain one negative switch and several live rules.
    # Exclude it only when every extracted clause is explicitly inactive.
    # Judge the atomic headline, not the concatenated conditions blob.
    negative_switch = bool(
        re.search(
            r"\b(?:mse|startup)\b.{0,80}\b(?:exemption|relaxation)\b.{0,40}(?:\bno\b|:\s*no\b)",
            text_normalized,
        )
        or re.search(
            r"\b(?:exemption|relaxation)\b.{0,40}:\s*no\b",
            text_normalized,
        )
        or re.search(
            r"\b(?:tpi|third[ -]?party inspection)\b.{0,40}(?:\bna\b|not applicable|:\s*na\b)",
            text_normalized,
        )
    )
    not_applicable = field_status == "not_applicable" or negative_switch or (bool(clauses) and all(
        any(marker in clause for marker in negative_markers)
        for clause in clauses
    ))
    bid_submission_proof = any(
        phrase in lifecycle_normalized
        for phrase in (
            "submit along with the bid",
            "submitted along with the bid",
            "submit with the bid",
            "submitted with techno-commercial",
            "submitted with techno commercial",
            "at the time of participating in the bid",
            "prior to part-i bid opening",
            "prior to part i bid opening",
        )
    ) or bool(
        re.search(
            r"\b(?:submit|submitted|furnish|furnished|provide|provided|upload|uploaded|enclose|enclosed)\b"
            r".{0,120}\b(?:along\s+with|with|in)\s+(?:the\s+)?"
            r"(?:techno[ -]?commercial(?:\s+(?:bid|offer))?|part[ -]?i(?:\s+(?:bid|offer))?|bid|offer)\b",
            lifecycle_normalized,
        )
    )
    atomic_post_award = any(
        phrase in normalized
        for phrase in (
            "after award",
            "post-award",
            "post award",
            "successful bidder shall",
            "within days of award",
            "at the time of delivery",
            "after placement of order",
            "after purchase order",
            "post-supply",
            "post supply",
            "l1 bidder",
            "prior to contract placement",
            "from po/call-off",
            "from purchase order",
            "supply order invoice",
            "invoice uploading",
            "uploading tax invoice",
            "delivery term",
            "quantity tolerance",
            "inspection call",
            "po validity",
        )
    )
    contextual_post_award = field_key == "regulatory_and_statutory_obligations" and any(
        phrase in lifecycle_normalized
        for phrase in (
            "breach of contract",
            "buyer may take suitable actions under the gem contract",
            "contract remedy",
            "applicable labour laws",
            "all labour laws",
            "delivery term",
            "quantity tolerance",
            "inspection call",
            "supply order invoice",
        )
    )
    source_post_award = any(
        phrase in source_normalized
        for phrase in (
            "after award",
            "post-award",
            "post award",
            "successful bidder shall",
            "within days of award",
            "at the time of delivery",
            "after placement of order",
            "after purchase order",
            "post-supply",
            "post supply",
            "supply order invoice",
            "uploading tax invoice",
            "inspection call",
            "breach of contract",
            "contract remedy",
        )
    )
    post_award = atomic_post_award or (
        (source_post_award or contextual_post_award) and not bid_submission_proof
    )
    price_threshold_condition = (
        "high-value declaration" in text_normalized
        or bool(re.search(
            r"(?:quoted|bid)\s+value.{0,40}(?:>|above|exceed|in excess).{0,20}\d+\s*(?:cr|crore)",
            semantic_normalized,
        ))
        or bool(re.search(
            r"(?:>|above|exceed).{0,20}10\s*(?:cr|crore)",
            semantic_normalized,
        ))
    )
    commercial = field_key in _COMMERCIAL_CONTEXT_FIELDS or price_threshold_condition or any(
        phrase in text_normalized
        for phrase in (
            "purchase preference",
            "order allocation",
            "l1 matching",
            "price preference",
            "quoted value exceeds",
            "quoted value above",
        )
    )
    delivery_or_inspection = any(
        phrase in text_normalized
        for phrase in (
            "at the time of delivery",
            "post-supply",
            "post supply",
            "delivery inspection",
            "inspection after supply",
            "delivery term",
            "quantity tolerance",
            "inspection call",
        )
    ) or (
        not bid_submission_proof
        and any(
            phrase in source_normalized
            for phrase in (
                "at the time of delivery",
                "post-supply",
                "post supply",
                "delivery inspection",
                "inspection after supply",
                "delivery term",
                "quantity tolerance",
                "inspection call",
            )
        )
    )
    if not bid_submission_proof and field_key == "regulatory_and_statutory_obligations" and any(
        phrase in lifecycle_normalized
        for phrase in (
            "invoice uploading",
            "gstr-1",
            "return filing",
            "input tax credit",
            "workmen's compensation",
            "buyer premises",
            "plant safety",
        )
    ):
        post_award = True
    scope = (
        "delivery_or_inspection"
        if delivery_or_inspection
        else ("post_award" if post_award else ("commercial_out_of_scope" if commercial else "technical_qualification"))
    )
    mse_reservation_applies = field_key == "mse_startup_policy" and any(
        phrase in normalized
        for phrase in ("reserved for mse: yes", "procurement is reserved for mses")
    )
    bidder_registration_condition = bool(re.search(
        r"\b(?:unregistered|newly registered|not registered)\s+(?:bidder|vendor|seller|supplier)s?\b",
        normalized,
    ))
    bidder_origin_condition = (
        bool(re.search(r"\bland[ -]border\b", normalized))
        and any(term in normalized for term in ("country", "countries", "competent authority"))
    )
    conditional = (
        field_key in _CONDITIONAL_POLICY_FIELDS
        and not (
            mse_reservation_applies
            and any(term in text_normalized for term in ("reserved for mse", "mse documentation", "udyam"))
        )
    ) or any(
        phrase in text_normalized
        for phrase in (
            "if claimed",
            "if applicable",
            "if required",
            "where applicable",
            "for supplies from",
            "exemption",
            "relaxation",
            "if quoted value",
            "if the quoted value",
            "when the quoted value",
        )
    ) or bidder_registration_condition or bidder_origin_condition or price_threshold_condition or bool(re.search(
        r"\bif\s+(?:the\s+)?(?:bidder|seller|supplier|quoted|bid|offer|proposal|submission|order|contract|invoice|claim)",
        semantic_normalized,
    ))
    scored = any(term in semantic_normalized for term in ("scored", "marks", "score awarded", "maximum score"))
    scored_gate = any(
        term in semantic_normalized
        for term in ("minimum qualifying score", "minimum score", "qualifying marks", "must score")
    )
    if not_applicable:
        applicability = "not_applicable"
    elif conditional:
        applicability = "conditional"
    else:
        applicability = "applicable"
    incomplete_tender_fact = (
        field_status in {"not_found", "unresolved", "ambiguous"}
        or not source_grounded
    )
    included = (
        scope == "technical_qualification"
        and not_applicable is False
        and incomplete_tender_fact is False
    )
    if not_applicable:
        applicability_rule = {
            "type": "tender_fact",
            "outcome": "not_applicable",
            "reason": "The effective tender explicitly marks this requirement inactive.",
        }
    elif field_key in {"mse_startup_policy", "mse_startup_relaxation", "msme_preference_applicable"}:
        applicability_rule = {
            "type": "bidder_claim",
            "claim": "mse_or_startup_benefit",
            "evaluate_when": "The bidder claims an MSE/MSME/startup relaxation, exemption, reservation, or preference.",
        }
    elif bidder_registration_condition:
        applicability_rule = {
            "type": "bidder_registration",
            "evaluate_when": "The bidder falls within the tender's stated unregistered/new-vendor condition.",
        }
    elif bidder_origin_condition:
        applicability_rule = {
            "type": "bidder_origin",
            "evaluate_when": "The bidder is from, or controlled through, a country covered by the tender's land-border rule.",
        }
    elif any(term in semantic_normalized for term in ("oem authorization", "manufacturer authorization", "authorization certificate")):
        applicability_rule = {
            "type": "bidder_role",
            "evaluate_when": "The bidder is not the manufacturer/OEM and relies on an OEM, dealer, distributor, or channel-partner route.",
        }
    elif any(term in normalized for term in ("first time supplier", "first-time supplier", "new supplier")):
        applicability_rule = {
            "type": "bidder_history",
            "evaluate_when": "The bidder has no accepted prior supply or performance evidence for the buyer under the tender rule.",
        }
    elif conditional:
        applicability_rule = {
            "type": "tender_condition",
            "evaluate_when": text,
        }
    else:
        applicability_rule = {"type": "universal"}
    materiality = (
        "post_award"
        if scope in {"post_award", "delivery_or_inspection"}
        else (
            "advisory"
            if incomplete_tender_fact
            else ("mandatory_blocking" if scored_gate else ("scored" if scored else ("conditional_blocking" if conditional else "mandatory_blocking")))
        )
    )
    requirement_kind = (
        "obligation"
        if scope in {"post_award", "delivery_or_inspection"}
        else ("scored" if scored else "gate")
    )
    return {
        "evaluation_scope": scope,
        "applicability": applicability,
        "materiality": materiality,
        "requirement_kind": requirement_kind,
        "included_by_default": included,
        "default_exclusion_reason": (
            "The effective tender states that this check is not applicable."
            if not_applicable
            else (
                "The tender fact is not grounded and must be resolved before it can become a bidder gate."
                if incomplete_tender_fact
                else ("Outside the current technical-qualification scope." if not included else "")
            )
        ),
        "applicability_rule": applicability_rule,
    }


def requirement_semantics_for_reviewed_atomic_field(
    field_key: str,
    atomic_value: str,
    source_references: list[dict[str, Any]],
    *,
    conditions: list[str] | None = None,
    exceptions: list[str] | None = None,
) -> dict[str, Any]:
    """Recompute source-grounded semantics when a frozen criterion is rehydrated."""
    source_text = " ".join(
        str(reference.get("quote") or reference.get("text") or "")
        for reference in source_references
        if isinstance(reference, dict)
    )
    return _requirement_semantics(
        field_key,
        atomic_value,
        conditions=conditions,
        exceptions=exceptions,
        source_text=source_text,
        source_grounded=bool(source_references),
    )


def _requirement_semantic_key(field_key: str, text: str) -> str:
    normalized = text.casefold()
    if field_key == "emd_amount_inr" or (
        field_key == "bid_security_and_fees"
        and any(term in normalized for term in ("emd", "earnest money", "bid security"))
    ):
        return "bhel.emd"
    if field_key in {"mse_startup_policy", "mse_startup_relaxation", "msme_preference_applicable"}:
        return "bhel.mse"
    if field_key in {"make_in_india_policy", "make_in_india_preference", "local_content_declaration"}:
        return "bhel.make_in_india"
    if field_key == "integrity_pact_required":
        return "bhel.integrity_pact"
    if field_key == "contractual_declarations":
        if "integrity pact" in normalized:
            return "bhel.integrity_pact"
        if "deviation" in normalized:
            return "bhel.deviation"
    if field_key in {"financial_capacity", "minimum_industry_experience", "similar_project_qualification"}:
        return "bhel.pqr"
    if field_key == "third_party_certification_assurance" and any(term in normalized for term in ("third party inspection", "third-party inspection", "tpia", "tpi")):
        return "bhel.third_party_inspection"
    return f"tender.{field_key}.{hashlib.sha1(text.casefold().encode()).hexdigest()[:12]}"


def _shared_call_budget_exhausted(field: dict[str, Any]) -> bool:
    return (
        field.get("status") == "unresolved"
        and str(field.get("notes") or "").strip() == _SHARED_CALL_BUDGET_NOTE
    )


__all__ = [
    "_COMMERCIAL_CONTEXT_FIELDS",
    "_CONDITIONAL_POLICY_FIELDS",
    "_SHARED_CALL_BUDGET_NOTE",
    "_atomic_requirement_values",
    "_requirement_semantic_key",
    "_requirement_semantics",
    "_shared_call_budget_exhausted",
    "requirement_semantics_for_reviewed_atomic_field",
]
