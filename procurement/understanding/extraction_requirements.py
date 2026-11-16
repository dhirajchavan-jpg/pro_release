"""Tender requirement consolidation and merge rules.

Implementation module for the public extraction compatibility facade.
"""

from __future__ import annotations

import json
import re
from typing import Any

from procurement.domain.document_dictionary import load_procurement_document_dictionary
from procurement.domain.schemas import ExtractedDocument
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match
from procurement.understanding.requirements import build_requirement_families, requirement_document_keys
from .extraction_normalization import (
    normalize_extraction_document,
    normalize_required_extraction_document,
    procurement_dictionary_entry_by_key,
)

def tender_requirement_consolidation_data(tender_docs: list[ExtractedDocument], merged: dict[str, Any]) -> dict[str, Any]:
    families = build_requirement_families(merged.get("requirements", []), merged.get("required_documents", []))
    requirements = deterministic_family_requirements(families)
    return {
        "requirements": requirements,
        "required_documents": consolidated_required_documents(merged.get("required_documents", []), requirements),
    }


TENDER_CONSOLIDATION_REQUIREMENT_ONLY_DOCUMENT_KEYS = {
    "billing_address",
    "ca_certificate_udin",
    "code_of_integrity",
    "drawings",
    "final_bill_ra_bill",
    "foreign_principal_agent_declaration",
    "gem_seller_category_evidence",
    "gst_tax_price_documents",
    "insolvency_liquidation_declaration",
    "invoice_payment_proof",
    "jv_consortium_declaration",
    "labour_compliance",
    "loa_contract_acceptance",
    "prebid_clarification_acknowledgement",
    "product_statutory_certificate",
    "safety_hse_undertaking",
    "sample_demo_prototype",
    "subcontractor_arrangement",
}


BQC_SPECIFIC_DOCUMENT_KEYS = {
    "completion_certificate",
    "experience_format",
    "final_bill_ra_bill",
    "invoice_payment_proof",
    "purchase_order_work_order",
    "similar_work_scope_proof",
}


def consolidated_required_documents(documents: list[Any], requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requirement_keys = {
        str(document.get("canonical_key") or "").strip()
        for requirement in requirements
        for document in requirement.get("required_documents", []) or []
        if isinstance(document, dict)
    }
    rows = []
    seen = set()
    for document in compact_required_documents(documents):
        key = str(document.get("canonical_key") or "").strip()
        if not key or key in seen:
            continue
        if not consolidated_document_key_is_required(key, requirement_keys):
            continue
        rows.append(document)
        seen.add(key)
    return rows


def consolidated_document_key_is_required(key: str, requirement_keys: set[str]) -> bool:
    if key in requirement_keys:
        return True
    if key == "bqc_qualification_documents":
        return not bool(requirement_keys & BQC_SPECIFIC_DOCUMENT_KEYS)
    return not document_key_needs_requirement_owner(key)


def document_key_needs_requirement_owner(key: str) -> bool:
    if key in TENDER_CONSOLIDATION_REQUIREMENT_ONLY_DOCUMENT_KEYS:
        return True
    entry = procurement_dictionary_entry_by_key(key)
    stage = normalize_for_match(str(entry.get("stage") or ""))
    required_when = normalize_for_match(str(entry.get("required_when") or ""))
    if stage in {"conditional", "post_award"}:
        return True
    return any(
        phrase in required_when
        for phrase in [
            "only when",
            "when bidder",
            "if bidder",
            "where bidder",
            "when tender asks",
            "when tender requires",
            "where tender requires",
            "when tender permits",
            "when tender references",
            "where tender references",
            "when tender accepts",
            "where executed value",
        ]
    )


FAMILY_REQUIREMENT_TEMPLATES = {
    "bid_submission": {
        "requirement": "Bid must be submitted through the prescribed online e-tendering portal with required digital signature certificate; bids submitted by other modes or after due date are not accepted.",
        "category": "submission_control",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["bid_submission_form", "reverse_auction_acceptance"],
    },
    "bid_separation_no_price": {
        "requirement": "Technical bid and commercial bid must remain separate; technical bid must not contain prices and must include a masked commercial copy where required.",
        "category": "submission_control",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["no_price_in_technical_bid", "financial_bid_boq"],
    },
    "bid_validity": {
        "requirement": "Offer validity must meet the tender-stated validity period from the tender opening or submission date.",
        "category": "submission_control",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["bid_validity_declaration", "bid_submission_form"],
    },
    "signed_tender_packet": {
        "requirement": "All schedules, formats, annexures, tender pages and corrigenda must be signed or stamped by an authorized signatory with authorization proof.",
        "category": "submission_control",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["tender_acceptance", "corrigendum_acknowledgement", "authorized_signatory_proof"],
    },
    "emd_bid_security": {
        "requirement": "Bid must include required EMD or bid security, or valid tender-permitted exemption evidence; EMD or bank guarantee must follow the prescribed instrument, amount and validity.",
        "category": "security",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["emd_bid_security", "bank_guarantee_instrument", "udyam_mse_certificate"],
    },
    "legal_identity": {
        "requirement": "Bidder must be a registered company, firm, LLP, trust, government-owned organization, or other tender-eligible legal entity and meet the tender-stated years-of-existence requirement.",
        "category": "legal_identity",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["business_registration", "gst_registration", "pan"],
    },
    "financial_capacity": {
        "requirement": "Bidder or allowed OEM/parent entity must have yearly turnover greater than the tender-stated threshold in each specified financial year and positive net worth in the required years; startup or MSE relaxation applies only when permitted by the tender and technical requirements are met.",
        "category": "financial_capacity",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["audited_financials", "turnover_certificate", "net_worth_certificate", "pan", "startup_recognition", "udyam_mse_certificate"],
    },
    "service_support": {
        "requirement": "Bidder must have the required support office, branch, or direct-support undertaking for the tender-stated service location.",
        "category": "service_support",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["methodology_work_plan", "technical_commercial_compliance"],
    },
    "oem_authorization": {
        "requirement": "Bidder must be OEM or authorized partner for the proposed solution; OEM and authorized partner cannot both bid for the same solution unless the tender allows it.",
        "category": "technical_authorization",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["oem_authorization"],
    },
    "blacklisting": {
        "requirement": "Bidder must not be blacklisted, debarred, or under holiday listing by the tender-stated government, PSU, bank, or buyer authorities.",
        "category": "commercial_legal",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["non_debarment_blacklisting"],
    },
    "prior_experience": {
        "requirement": "Bidder or allowed OEM must have tender-stated similar supply, work, service, or live-solution experience for the required client sector or operating model within the specified lookback period, supported by experience format, order, completion, engagement, or user-acceptance proof.",
        "category": "prior_experience",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["experience_format", "purchase_order_work_order", "completion_certificate", "similar_work_scope_proof"],
    },
    "integrity_pact": {
        "requirement": "Bidder must submit signed Integrity Pact in the prescribed tender format, including stamp-paper or signatory requirements where specified.",
        "category": "commercial_legal",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["integrity_pact"],
    },
    "conflict_litigation": {
        "requirement": "Bidder or OEM must have no litigation pending against the buyer in any court as on bidding date and must have no tender-disqualifying conflict of interest.",
        "category": "commercial_legal",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["conflict_of_interest"],
    },
    "npa_solvency": {
        "requirement": "Bidder or relevant parent entity must not have been declared NPA or otherwise financially/legally impaired where the tender treats this as an eligibility condition.",
        "category": "financial_capacity",
        "stage": "eligibility",
        "failure_effect": "reject_technical",
        "required_document_keys": ["solvency_bank_certificate"],
    },
    "nda_confidentiality": {
        "requirement": "Bidder must sign Non-Disclosure Agreement or confidentiality undertaking in the prescribed tender format and maintain confidentiality of buyer information.",
        "category": "commercial_legal",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["confidentiality_nda"],
    },
    "land_border": {
        "requirement": "Bidder from a country sharing land border with India must be registered with the competent authority or submit the prescribed land-border certificate as required by the tender.",
        "category": "preference_policy",
        "stage": "bid_stage",
        "failure_effect": "reject_unresponsive",
        "required_document_keys": ["land_border_declaration"],
    },
    "local_content": {
        "requirement": "PPP-MII or local-content certificate must be submitted for the offered goods or services as per the tender's local-content format and auditor certification requirement.",
        "category": "preference_policy",
        "stage": "bid_stage",
        "failure_effect": "reject_or_preference_denied_based_on_tender",
        "required_document_keys": ["local_content_declaration", "local_content_auditor_certificate"],
    },
    "technical_proposal": {
        "requirement": "Bidder must submit technical proposal covering project plan, execution schedule, methodology, solution architecture, hardware/software requirements, team profile and required technical undertakings.",
        "category": "technical_quality",
        "stage": "technical_evaluation",
        "failure_effect": "reject_technical",
        "required_document_keys": ["methodology_work_plan", "technical_spec_compliance", "technical_commercial_compliance", "manpower_deployment", "equipment_deployment", "datasheet_catalogue"],
    },
    "technical_scoring": {
        "requirement": "Technical score must be evaluated under the tender's declared scoring criteria, including relevant experience, skilled resources, bidder/OEM capability and technical presentation evidence.",
        "category": "technical_scoring",
        "stage": "technical_evaluation",
        "failure_effect": "block_financial_opening",
        "required_document_keys": ["experience_format", "manpower_deployment", "datasheet_catalogue", "methodology_work_plan"],
    },
    "technical_cutoff": {
        "requirement": "Bidder must meet the tender-stated technical qualification cut-off before commercial or financial evaluation; buyer relaxation, if any, must follow the tender or corrigendum.",
        "category": "technical_scoring",
        "stage": "technical_evaluation",
        "failure_effect": "block_financial_opening",
        "required_document_keys": ["technical_spec_compliance", "methodology_work_plan"],
    },
    "functional_specification": {
        "requirement": "Functional specification compliance must be submitted; all mandatory functionalities must be accepted or demonstrated as required by the tender.",
        "category": "technical_quality",
        "stage": "technical_evaluation",
        "failure_effect": "reject_technical",
        "required_document_keys": ["technical_spec_compliance"],
    },
    "financial_bid": {
        "requirement": "Commercial or financial bid must be in the prescribed format, quote all required items, use the tender-stated currency, and contain no conditions or unauthorized alterations.",
        "category": "financial_bid",
        "stage": "financial",
        "failure_effect": "block_financial_opening",
        "required_document_keys": ["financial_bid_boq"],
    },
    "performance_security": {
        "requirement": "Successful bidder must furnish performance security or performance bank guarantee for the tender-stated percentage of contract value within the prescribed timeline from purchase order or indent, valid for the required contract period plus claim period.",
        "category": "security",
        "stage": "post_award",
        "failure_effect": "manual_review",
        "required_document_keys": ["performance_security_pbg", "bank_guarantee_instrument"],
    },
    "delivery_timeline": {
        "requirement": "Solution, supply, or work must be implemented within the tender-stated weeks or timeline from purchase order, contract award, or other specified start date, including buyer-shortlisted milestones, processes, or phased delivery requirements.",
        "category": "delivery_timeline",
        "stage": "post_award",
        "failure_effect": "manual_review",
        "required_document_keys": ["methodology_work_plan"],
    },
    "scope_delivery": {
        "requirement": "Vendor must provide tender-required onsite or facility-management support, including required support resources, service coverage, security remediation, training, and operational processes.",
        "category": "scope_delivery",
        "stage": "technical_evaluation",
        "failure_effect": "reject_technical",
        "required_document_keys": ["manpower_deployment", "methodology_work_plan", "technical_spec_compliance"],
    },
}


FAMILY_TEMPLATE_ORDER = [
    "bid_submission",
    "bid_separation_no_price",
    "bid_validity",
    "signed_tender_packet",
    "emd_bid_security",
    "legal_identity",
    "financial_capacity",
    "service_support",
    "oem_authorization",
    "blacklisting",
    "prior_experience",
    "integrity_pact",
    "conflict_litigation",
    "npa_solvency",
    "nda_confidentiality",
    "land_border",
    "local_content",
    "technical_proposal",
    "technical_scoring",
    "technical_cutoff",
    "functional_specification",
    "financial_bid",
    "performance_security",
    "delivery_timeline",
    "scope_delivery",
]


def deterministic_family_requirements(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(family.get("family_id")): family for family in families if family.get("family_id")}
    rows = []
    for family_id in FAMILY_TEMPLATE_ORDER:
        family = by_id.get(family_id)
        if not family and family_id == "technical_cutoff" and "technical_scoring" in by_id:
            family = by_id["technical_scoring"]
        if not family and family_id == "functional_specification" and "technical_proposal" in by_id:
            family = by_id["technical_proposal"]
        if not family:
            continue
        template = FAMILY_REQUIREMENT_TEMPLATES[family_id]
        available_keys = set(family.get("required_document_keys") or [])
        template_keys = template["required_document_keys"]
        keys = [key for key in template_keys if not available_keys or key in available_keys]
        if not keys:
            keys = template_keys[:1]
        refs = representative_family_refs(family)
        requirement_text = source_aware_family_requirement(family_id, family, str(template["requirement"]))
        rows.append(
            {
                "requirement": requirement_text,
                "category": template["category"],
                "stage": template["stage"],
                "mandatory": True,
                "failure_effect": template["failure_effect"],
                "required_documents": [{"canonical_key": key} for key in keys],
                "extraction_status": "extracted",
                "missing_detail": "",
                "source_refs": refs,
            }
        )
    return rows


def representative_family_refs(family: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for row in family.get("evidence_rows") or []:
        for ref in row.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            compact = {
                "document_id": ref.get("document_id", ""),
                "page_number": ref.get("page_number"),
                "quote": str(ref.get("quote") or "")[:160],
            }
            if compact not in refs:
                refs.append(compact)
            if len(refs) >= 2:
                return refs
    return refs


SOURCE_AWARE_FAMILIES = {
    "financial_capacity",
    "prior_experience",
    "performance_security",
    "delivery_timeline",
    "technical_cutoff",
    "service_support",
}


def source_aware_family_requirement(family_id: str, family: dict[str, Any], fallback: str) -> str:
    if family_id not in SOURCE_AWARE_FAMILIES:
        return fallback
    candidates = []
    quote_detail_families = {"financial_capacity", "prior_experience", "performance_security", "technical_cutoff", "delivery_timeline"}
    for row in family.get("evidence_rows") or []:
        requirement = str(row.get("requirement") or "")
        if not clean_text(requirement):
            continue
        quote_text = ""
        if family_id in quote_detail_families:
            quote_text = " ".join(str(ref.get("quote") or "") for ref in row.get("source_refs") or [] if isinstance(ref, dict))
        candidates.append(clean_text(f"{requirement} {quote_text}"))
    if not candidates:
        return fallback
    if family_id == "financial_capacity":
        best = best_source_candidate(candidates, source_terms_for_family(family_id))
        return compact_financial_capacity_requirement(best, fallback)
    if family_id == "prior_experience":
        specific_candidates = [
            candidate
            for candidate in candidates
            if has_family_specific_source_fact(family_id, candidate)
            and (
                source_client_scope_phrase(candidate)
                or source_lookback_phrase(candidate)
                or source_operating_model_phrase(candidate)
                or re.search(r"\blive\b", normalize_for_match(candidate))
            )
        ]
        best = best_source_candidate(specific_candidates or candidates, source_terms_for_family(family_id))
        return compact_prior_experience_requirement(best, fallback)
    if family_id == "performance_security":
        guarantee = best_source_candidate(candidates, ["3%", "contract price", "contract value", "percentage"])
        validity = best_source_candidate(candidates, ["valid", "claim period", "six months", "contract period"])
        if guarantee and validity and guarantee != validity:
            return compact_performance_security_requirement(f"{guarantee} {validity}", fallback)
        best = guarantee or validity
        return compact_performance_security_requirement(best, fallback)
    if family_id == "technical_cutoff":
        best = best_source_candidate(candidates, source_terms_for_family(family_id))
        return compact_technical_cutoff_requirement(best, fallback)
    if family_id == "delivery_timeline":
        candidates = [
            candidate
            for candidate in candidates
            if has_family_specific_source_fact(family_id, candidate)
            and not any(term in normalize_for_match(candidate) for term in ["performance bank guarantee", "pbg", "contract price"])
        ]
        best = best_source_candidate(candidates, source_terms_for_family(family_id))
        return compact_delivery_timeline_requirement(best, fallback)
    best = best_source_candidate(candidates, source_terms_for_family(family_id))
    if not best:
        return fallback
    return best if has_family_specific_source_fact(family_id, best) else fallback


def best_source_candidate(candidates: list[str], terms: list[str]) -> str:
    return max(candidates, key=lambda text: candidate_term_score(text, terms), default="")


def candidate_term_score(text: str, terms: list[str]) -> int:
    normalized = normalize_for_match(text)
    score = sum(4 for term in terms if normalize_for_match(term) in normalized)
    score += len(re.findall(r"\b\d+(?:\.\d+)?%?|\b20\d{2}\b|\b\d+\s*(?:crore|lakh|weeks|months|years)\b", normalized))
    score += min(len(keyword_tokens(normalized)), 50) // 5
    if any(term in normalized for term in ["payment", "invoice", "uat sign", "broker", "agent"]):
        score -= 10
    return score


def source_terms_for_family(family_id: str) -> list[str]:
    return {
        "legal_identity": ["registered company", "llp", "trust", "government owned", "existence", "certificate of incorporation"],
        "financial_capacity": ["turnover", "net worth", "financial years", "startup", "relaxation"],
        "prior_experience": ["experience", "live", "similar", "client", "sector", "model", "completion", "purchase order", "work order", "satisfactory"],
        "conflict_litigation": ["litigation", "court", "buyer", "conflict of interest"],
        "npa_solvency": ["npa", "declared", "bank", "bfsi", "solvency"],
        "delivery_timeline": ["implemented", "implementation", "weeks", "purchase order", "phase", "timeline"],
        "technical_cutoff": ["cut off", "cut-off", "minimum score", "technical score", "commercial evaluation", "financial evaluation"],
        "service_support": ["support office", "branch", "direct support", "service location", "onsite support"],
    }.get(family_id, [])


def has_family_specific_source_fact(family_id: str, text: str) -> bool:
    normalized = normalize_for_match(text)
    if family_id == "technical_cutoff":
        return ("cut off" in normalized or "cutoff" in normalized or "minimum score" in normalized or "technical score" in normalized) or (
            bool(re.search(r"\b\d+(?:\.\d+)?%", normalized))
            and any(term in normalized for term in ["technical", "commercial evaluation", "financial evaluation", "qualify", "qualification"])
        )
    if family_id == "delivery_timeline":
        return bool(re.search(r"\b\d+\s*(?:weeks|months|days)\b", normalized)) or (
            "timeline" in normalized and any(term in normalized for term in ["purchase order", "work order", "award"])
        )
    if family_id == "prior_experience":
        return "experience" in normalized and any(
            term in normalized
            for term in ["similar", "client", "sector", "model", "live", "completion", "purchase order", "work order", "satisfactory"]
        )
    if family_id == "service_support":
        return any(term in normalized for term in ["support office", "branch", "direct support", "onsite support", "service location"])
    if re.search(r"\b\d+(?:\.\d+)?%?|\b20\d{2}\b|\b\d+\s*(?:crore|lakh|weeks|months|years)\b", normalized):
        return True
    return any(
        term in normalized
        for term in [
            "companies act",
            "government owned",
            "certificate of incorporation",
            "declared npa",
            "litigation pending",
            "support office",
            "direct support",
            "claim period",
            "contract price",
            "contract value",
        ]
    )


def compact_financial_capacity_requirement(text: str, fallback: str) -> str:
    normalized = normalize_for_match(text)
    if "turnover" not in normalized and "net worth" not in normalized:
        return fallback
    threshold = re.search(r"(?:Rs\.?\s*)?\d+(?:\.\d+)?\s*crores?", text, re.IGNORECASE)
    years = re.findall(r"\b20\d{2}-\d{2}\b", text)
    relaxation = re.search(r"\b\d+(?:\.\d+)?%\b", text)
    parts = ["Bidder or OEM must have yearly turnover"]
    if threshold:
        parts.append(f"greater than {threshold.group(0)}")
    else:
        parts.append("greater than the tender-stated threshold")
    if years:
        parts.append(f"in each of FY {', '.join(dict.fromkeys(years))}")
    else:
        parts.append("in each specified financial year")
    if "net worth" in normalized:
        parts.append("and positive net worth in each of the required financial years")
    if "startup" in normalized and relaxation:
        parts.append(f"; startups can get {relaxation.group(0)} turnover relaxation if permitted by the tender")
    return clean_text(" ".join(parts) + ".")


def source_lookback_phrase(text: str) -> str:
    match = re.search(
        r"\b(?:last|past|previous|preceding)\s+\d+\s+years?\b|\bwithin\s+(?:the\s+)?(?:last|past|previous|preceding)\s+\d+\s+years?\b",
        text,
        re.IGNORECASE,
    )
    return match.group(0).lower() if match else ""


def source_client_scope_phrase(text: str) -> str:
    match = re.search(
        r"\bto\s+(?:a|an|any|the)\s+([^.;,\n]{3,90}?)(?=\s+(?:in|under|within|with|during|for)\b|[.;,\n]|$)",
        text,
        re.IGNORECASE,
    )
    return clean_text(match.group(1)).lower() if match else ""


def source_operating_model_phrase(text: str) -> str:
    acronym_model = re.search(r"\b[A-Z]{2,}(?:\s*/\s*[A-Z]{2,})+\s+model\b", text, re.IGNORECASE)
    if acronym_model:
        return re.sub(r"\s*/\s*", "/", clean_text(acronym_model.group(0)))
    match = re.search(r"\b[A-Z0-9][A-Z0-9 /&+-]{1,35}\s+model\b", text, re.IGNORECASE)
    if not match:
        return ""
    phrase = clean_text(match.group(0))
    return phrase if len(phrase.split()) <= 5 else ""


def delivery_start_trigger_phrase(text: str) -> str:
    normalized = normalize_for_match(text)
    if "date of placing order" in normalized:
        return "the date of placing order"
    if "purchase order" in normalized:
        return "purchase order"
    if "work order" in normalized:
        return "work order"
    if "contract award" in normalized or "award" in normalized:
        return "contract award"
    return "the tender-stated start date"


def compact_prior_experience_requirement(text: str, fallback: str) -> str:
    normalized = normalize_for_match(text)
    if not has_family_specific_source_fact("prior_experience", text):
        return fallback
    lookback = source_lookback_phrase(text)
    client_scope = source_client_scope_phrase(text)
    model = source_operating_model_phrase(text)
    scope = f" to a {client_scope}" if client_scope else " for the tender-stated client sector or similar work scope"
    model_text = f" in {model}" if model else ""
    period = f" within the {lookback}" if lookback else " within the tender-stated lookback period"
    return (
        f"Bidder or OEM must have tender-stated similar experience in India{scope}{model_text}{period}, "
        "supported by completion, engagement, order or user-acceptance proof."
    )


def compact_technical_cutoff_requirement(text: str, fallback: str) -> str:
    normalized = normalize_for_match(text)
    if not has_family_specific_source_fact("technical_cutoff", text):
        return fallback
    percentages = list(dict.fromkeys(re.findall(r"\b\d+(?:\.\d+)?%", text)))
    parts = ["Bidder must meet technical qualification cut-off"]
    if percentages:
        parts.append(f"of {percentages[0]}")
    if "commercial evaluation" in normalized:
        parts.append("for commercial evaluation")
    elif "financial evaluation" in normalized:
        parts.append("before financial evaluation")
    else:
        parts.append("for commercial or financial evaluation")
    if len(percentages) > 1 or any(term in normalized for term in ["relax", "sole discretion", "next highest", "edge case", "edge-case"]):
        return clean_text(" ".join(parts) + "; tender relaxations or edge-case qualification rules apply only as stated in the tender.")
    return clean_text(" ".join(parts) + ".")


def compact_performance_security_requirement(text: str, fallback: str) -> str:
    normalized = normalize_for_match(text)
    if not any(term in normalized for term in ["guarantee", "performance security", "pbg"]):
        return fallback
    percent = re.search(r"\b\d+(?:\.\d+)?%", text)
    claim = re.search(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*months?\b", text, re.IGNORECASE)
    parts = ["Successful bidder must furnish performance bank guarantee"]
    if percent:
        parts.append(f"of {percent.group(0)} of contract value")
    if "valid" in normalized or "claim period" in normalized:
        parts.append("valid for the contract period")
    if claim:
        parts.append(f"plus {claim.group(0).lower()} claim period")
    parts.append("within the prescribed tender timeline.")
    return clean_text(" ".join(parts))


def compact_delivery_timeline_requirement(text: str, fallback: str) -> str:
    normalized = normalize_for_match(text)
    if not has_family_specific_source_fact("delivery_timeline", text):
        return fallback
    period = re.search(r"\b\d+\s*(?:weeks|months|days)\b", text, re.IGNORECASE)
    if period and any(term in normalized for term in ["placing order", "purchase order", "work order", "award"]):
        return f"Tendered solution must be implemented within {period.group(0).lower()} from {delivery_start_trigger_phrase(text)}."
    if period:
        return f"Solution, supply, or work must be implemented within {period.group(0).lower()} from the tender-stated start date."
    return fallback


def compact_required_documents(documents: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for document in documents:
        if not isinstance(document, dict):
            document = model_dump(document)
        key = str(document.get("canonical_key") or "").strip()
        if not key:
            continue
        rows.append(
            {
                "canonical_key": key,
                "display_name": document.get("display_name", ""),
                "document_type": document.get("document_type", "unknown"),
                "category": document.get("category", "other"),
                "stage": document.get("stage", "bid_stage"),
                "checks": document.get("checks", [])[:12],
            }
        )
    return rows

def apply_requirement_consolidation_result(merged: dict[str, Any], consolidated: dict[str, Any]) -> None:
    input_requirement_count = len(merged.get("requirements", []))
    requirements = consolidated.get("requirements") if isinstance(consolidated, dict) else None
    if not isinstance(requirements, list) or not requirements:
        return
    cleaned = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        text = clean_text(str(item.get("requirement") or ""))
        if len(text) < 20:
            continue
        item["requirement"] = text
        item["required_documents"] = [normalize_extraction_document(doc) for doc in item.get("required_documents") or []]
        item.setdefault("category", "other")
        item.setdefault("stage", "other")
        item.setdefault("mandatory", True)
        item.setdefault("failure_effect", "reject_unresponsive")
        item.setdefault("extraction_status", "extracted")
        item["missing_detail"] = "" if item.get("missing_detail") is None else str(item.get("missing_detail") or "")
        item["source_refs"] = item.get("source_refs") or []
        cleaned.append(item)
    if not cleaned:
        return
    for index, item in enumerate(cleaned, start=1):
        item["requirement_id"] = f"T1-C{index}"
    merged["requirements"] = cleaned
    documents = consolidated.get("required_documents") if isinstance(consolidated, dict) else None
    if isinstance(documents, list):
        merged["required_documents"] = [normalize_required_extraction_document(document) for document in documents]
    merged.setdefault("audit", {})["requirement_consolidation"] = {
        "enabled": True,
        "input_requirement_count": input_requirement_count,
        "output_requirement_count": len(cleaned),
    }

def merge_tender_dict(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        current = target.get(key)
        if current in (None, "", [], {}):
            target[key] = value
        elif isinstance(current, dict) and isinstance(value, dict):
            merge_tender_dict(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            seen = {json.dumps(item, sort_keys=True, default=str) for item in current}
            for item in value:
                marker = json.dumps(item, sort_keys=True, default=str)
                if marker not in seen:
                    current.append(item)
                    seen.add(marker)
        elif len(str(value)) > len(str(current)) and key in {"scope_summary", "evaluation_method"}:
            target[key] = value


def is_keepable_tender_requirement(requirement: dict[str, Any]) -> bool:
    text = normalize_for_match(str(requirement.get("requirement") or ""))
    if len(text) < 20:
        return False
    doc_keys = requirement_document_keys(requirement)
    if "selected bidder" in text or "successful bidder" in text:
        return bool(doc_keys & {"performance_security_pbg", "bank_guarantee_instrument"})
    if any(term in text for term in ["minimum wages", "labour laws", "aadhaar act"]):
        return False
    if doc_keys:
        return True
    return any(
        term in text
        for term in [
            "reject",
            "disqualif",
            "eligible",
            "technical score",
            "cut off",
            "financial opening",
            "commercial evaluation",
            "quote for all",
            "all columns",
        ]
    )


def merge_requirement_row(rows: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    for index, existing in enumerate(rows):
        if requirements_are_similar(existing, candidate):
            if requirement_quality_score(candidate) > requirement_quality_score(existing):
                rows[index] = candidate
            return
    rows.append(candidate)


def requirements_are_similar(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_text = str(left.get("requirement") or "")
    right_text = str(right.get("requirement") or "")
    if normalize_for_match(left_text) == normalize_for_match(right_text):
        return True
    left_docs = requirement_document_keys(left)
    right_docs = requirement_document_keys(right)
    overlap = left_docs & right_docs
    score = requirement_token_overlap(left_text, right_text)
    if overlap:
        return score >= 0.34
    return score >= 0.58


def requirement_token_overlap(left: str, right: str) -> float:
    left_tokens = keyword_tokens(left)
    right_tokens = keyword_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def requirement_quality_score(requirement: dict[str, Any]) -> int:
    text = normalize_for_match(str(requirement.get("requirement") or ""))
    quote_text = normalize_for_match(" ".join(str(ref.get("quote") or "") for ref in requirement.get("source_refs") or [] if isinstance(ref, dict)))
    score = len(requirement_document_keys(requirement)) * 3
    score += min(len(keyword_tokens(text)), 40)
    score += sum(5 for term in ["must", "shall", "reject", "disqualif", "score", "cut off", "financial opening"] if term in text)
    if quote_text:
        score += 4
    if re.search(r"\b\d+\s*(?:weeks|months|days|years|crores?|lakhs?)\b|\b\d+(?:\.\d+)?%", quote_text):
        score += 10
    if "tender-stated" in text and not re.search(r"\b\d+\s*(?:weeks|months|days|years|crores?|lakhs?)\b|\b\d+(?:\.\d+)?%", f"{text} {quote_text}"):
        score -= 6
    return score
