"""Deterministic tender requirement grouping."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.serialization import model_dump
from procurement.domain.text import normalize_for_match


def requirement_document_keys(requirement: dict[str, Any]) -> set[str]:
    return {
        str(document.get("canonical_key") or "").strip()
        for document in requirement.get("required_documents") or []
        if isinstance(document, dict) and str(document.get("canonical_key") or "").strip()
    }


def compact_requirements_for_consolidation(requirements: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            requirement = model_dump(requirement)
        rows.append(
            {
                "requirement": str(requirement.get("requirement") or "")[:500],
                "category": requirement.get("category", "other"),
                "stage": requirement.get("stage", "other"),
                "mandatory": requirement.get("mandatory", True),
                "failure_effect": requirement.get("failure_effect", ""),
                "required_document_keys": sorted(requirement_document_keys(requirement)),
                "source_refs": [
                    {
                        "document_id": ref.get("document_id", ""),
                        "page_number": ref.get("page_number"),
                        "quote": str(ref.get("quote") or "")[:160],
                    }
                    for ref in requirement.get("source_refs", [])[:2]
                    if isinstance(ref, dict)
                ],
            }
        )
    return rows


def requirement_family(requirement: dict[str, Any]) -> tuple[str | None, str, str]:
    text = normalize_for_match(str(requirement.get("requirement") or ""))
    keys = set(requirement.get("required_document_keys") or []) | requirement_document_keys(requirement)
    stage = str(requirement.get("stage") or "other")
    category = str(requirement.get("category") or "other")
    if keys & {"code_of_integrity", "foreign_principal_agent_declaration", "labour_compliance", "invoice_payment_proof", "final_bill_ra_bill", "loa_contract_acceptance"}:
        return None, category, stage
    if keys & {"no_price_in_technical_bid"} or ("masked" in text and "commercial" in text) or ("technical bid" in text and "commercial bid" in text):
        return "bid_separation_no_price", "submission_control", "bid_stage"
    if keys & {"bid_validity_declaration"} or "180 days" in text or "offer valid" in text:
        return "bid_validity", "submission_control", "bid_stage"
    if keys & {"tender_acceptance", "corrigendum_acknowledgement", "authorized_signatory_proof"}:
        if "reverse auction" in text:
            return "reverse_auction", "portal_specific", "bid_stage"
        return "signed_tender_packet", "submission_control", "bid_stage"
    if keys & {"bid_submission_form"} or any(term in text for term in ["portal", "technical bid", "tender offer cover letter", "digital certificate", "class ii", "class iii"]):
        return "bid_submission", "submission_control", "bid_stage"
    if keys & {"emd_bid_security", "bank_guarantee_instrument"}:
        return "emd_bid_security", "security", "bid_stage"
    if keys & {"performance_security_pbg"}:
        return "performance_security", "security", "post_award"
    if keys & {"business_registration", "gst_registration"}:
        return "legal_identity", "legal_identity", "eligibility"
    if keys & {"audited_financials", "turnover_certificate", "net_worth_certificate", "pan", "startup_recognition"}:
        return "financial_capacity", "financial_capacity", "eligibility"
    if "support office" in text or "direct support" in text or ("branch" in text and "support" in text):
        return "service_support", "service_support", "eligibility"
    if keys & {"oem_authorization"}:
        return "oem_authorization", "technical_authorization", "eligibility"
    if keys & {"non_debarment_blacklisting"}:
        return "blacklisting", "commercial_legal", "eligibility"
    if keys & {"conflict_of_interest"}:
        return "conflict_litigation", "commercial_legal", "eligibility"
    if keys & {"solvency_bank_certificate", "insolvency_liquidation_declaration"}:
        return "npa_solvency", "financial_capacity", "eligibility"
    if keys & {"experience_format", "purchase_order_work_order", "completion_certificate", "similar_work_scope_proof"}:
        if any(term in text for term in ["score", "marks", "technical evaluation", "scoring"]):
            return "technical_scoring", "technical_scoring", "technical_evaluation"
        return "prior_experience", "prior_experience", "eligibility"
    if keys & {"integrity_pact"}:
        return "integrity_pact", "commercial_legal", "eligibility"
    if keys & {"confidentiality_nda"}:
        return "nda_confidentiality", "commercial_legal", "bid_stage"
    if keys & {"land_border_declaration"}:
        return "land_border", "preference_policy", "bid_stage"
    if keys & {"local_content_declaration", "local_content_auditor_certificate"}:
        return "local_content", "preference_policy", "bid_stage"
    if (
        "cut off" in text
        or "cutoff" in text
        or "technical score" in text
        or ("marks" in text and "technical" in text)
        or (re.search(r"\b\d+(?:\.\d+)?%", text) and any(term in text for term in ["technical", "commercial evaluation", "financial evaluation", "qualification"]))
    ):
        return "technical_scoring", "technical_scoring", "technical_evaluation"
    if "functional specification" in text or "annexure h" in text:
        return "functional_specification", "technical_quality", "technical_evaluation"
    if re.search(r"\bwithin\b.*\b(?:weeks|months|days)\b", text) or any(term in text for term in ["date of placing order", "purchase order", "remaining processes", "implementation timeline"]):
        return "delivery_timeline", "delivery_timeline", "post_award"
    if any(term in text for term in ["onsite support", "facility management", "24x7", "l1", "l2", "vapt", "siem", "training for buyer", "training for client", "user training"]):
        return "scope_delivery", "scope_delivery", "technical_evaluation"
    if keys & {"methodology_work_plan", "technical_spec_compliance", "manpower_deployment", "equipment_deployment", "datasheet_catalogue"}:
        if stage == "post_award" or re.search(r"\b\d+\s*(?:weeks|months|days)\b", text) or any(
            term in text for term in ["implementation timeline", "project timeline", "date of placing order"]
        ):
            return "delivery_timeline", "delivery_timeline", "post_award"
        if any(term in text for term in ["onsite", "24x7", "facility management", "l1", "l2"]) or ("support" in text and any(term in text for term in ["resource", "facility", "onsite", "service location", "branch"])):
            return "scope_delivery", "scope_delivery", "technical_evaluation"
        return "technical_proposal", "technical_quality", "technical_evaluation"
    if keys & {"financial_bid_boq"} or "commercial bid" in text or "price bid" in text:
        return "financial_bid", "financial_bid", "financial"
    if keys & {"performance_security_pbg"}:
        return "performance_security", "security", "post_award"
    return None, category, stage


def build_requirement_families(requirements: list[Any], required_documents: list[Any]) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for requirement in compact_requirements_for_consolidation(requirements):
        family_id, category, stage = requirement_family(requirement)
        if not family_id:
            continue
        family = families.setdefault(
            family_id,
            {
                "family_id": family_id,
                "category": category,
                "stage": stage,
                "required_document_keys": [],
                "evidence_rows": [],
            },
        )
        family["required_document_keys"] = sorted(
            set(family["required_document_keys"]) | set(requirement.get("required_document_keys") or [])
        )
        if len(family["evidence_rows"]) < 8:
            family["evidence_rows"].append(
                {
                    "requirement": requirement.get("requirement", ""),
                    "required_document_keys": requirement.get("required_document_keys", []),
                    "source_refs": requirement.get("source_refs", []),
                }
            )
    document_keys = {
        str((document if isinstance(document, dict) else model_dump(document)).get("canonical_key") or "").strip()
        for document in required_documents
    }
    for family_id, keys, category, stage in [
        ("bid_submission", {"bid_submission_form", "reverse_auction_acceptance"}, "submission_control", "bid_stage"),
        ("bid_separation_no_price", {"no_price_in_technical_bid", "financial_bid_boq"}, "submission_control", "bid_stage"),
        ("bid_validity", {"bid_validity_declaration"}, "submission_control", "bid_stage"),
        ("signed_tender_packet", {"tender_acceptance", "corrigendum_acknowledgement", "authorized_signatory_proof"}, "submission_control", "bid_stage"),
        ("emd_bid_security", {"emd_bid_security", "bank_guarantee_instrument", "udyam_mse_certificate"}, "security", "bid_stage"),
        ("legal_identity", {"business_registration", "gst_registration", "pan"}, "legal_identity", "eligibility"),
        ("financial_capacity", {"audited_financials", "turnover_certificate", "net_worth_certificate", "pan", "startup_recognition"}, "financial_capacity", "eligibility"),
        ("oem_authorization", {"oem_authorization"}, "technical_authorization", "eligibility"),
        ("blacklisting", {"non_debarment_blacklisting"}, "commercial_legal", "eligibility"),
        ("conflict_litigation", {"conflict_of_interest"}, "commercial_legal", "eligibility"),
        ("npa_solvency", {"solvency_bank_certificate"}, "financial_capacity", "eligibility"),
        ("prior_experience", {"experience_format", "purchase_order_work_order", "completion_certificate", "similar_work_scope_proof"}, "prior_experience", "eligibility"),
        ("integrity_pact", {"integrity_pact"}, "commercial_legal", "eligibility"),
        ("nda_confidentiality", {"confidentiality_nda"}, "commercial_legal", "bid_stage"),
        ("land_border", {"land_border_declaration"}, "preference_policy", "bid_stage"),
        ("local_content", {"local_content_declaration", "local_content_auditor_certificate"}, "preference_policy", "bid_stage"),
        ("technical_proposal", {"methodology_work_plan", "technical_spec_compliance", "technical_commercial_compliance", "manpower_deployment", "equipment_deployment", "datasheet_catalogue"}, "technical_quality", "technical_evaluation"),
        ("financial_bid", {"financial_bid_boq"}, "financial_bid", "financial"),
        ("performance_security", {"performance_security_pbg"}, "security", "post_award"),
    ]:
        matched = sorted(document_keys & keys)
        if matched:
            family = families.setdefault(
                family_id,
                {
                    "family_id": family_id,
                    "category": category,
                    "stage": stage,
                    "required_document_keys": [],
                    "evidence_rows": [],
                },
            )
            family["required_document_keys"] = sorted(set(family["required_document_keys"]) | set(matched))
    return list(families.values())
