"""Static catalogs and token normalization for requirement evidence."""

from __future__ import annotations

import re
from typing import Any


_EVIDENCE_MATCH_STOPWORDS = {
    "and", "the", "for", "from", "with", "that", "this", "must", "shall",
    "required", "requirement", "requirements", "valid", "proof", "evidence",
    "document", "documents", "certificate", "certification", "registration",
    "declaration", "annexure", "bid", "bidder", "tender", "submitted", "submission",
    "bidders", "vendor", "vendors", "seller", "sellers", "supplier", "suppliers",
    "applicable", "details", "supporting", "compliance", "technical",
    # These describe how an artifact is completed, not which artifact it is.
    # Treating them as routing signals can make one signed declaration inherit
    # every other signed declaration from the same reviewed field.
    "authorised", "authorized", "completed", "duly", "filled", "signed",
    "attested", "attestation", "certified", "certify", "executed",
    "seal", "sealed", "stamp", "stamped", "stamping", "signature",
    "signatory", "witnessed",
    # Relative words describe a condition, not the artifact being requested.
    "above", "below",
}

_EVIDENCE_TOKEN_ALIASES = {
    "gst": "gstin",
    "factories": "factory",
    "factory": "factory",
    "licence": "license",
    "licences": "license",
    "licensed": "license",
    "licensing": "license",
    "licenses": "license",
    "regulations": "regulation",
    "regulatory": "regulation",
}

def _evidence_match_tokens(value: str) -> set[str]:
    """Return small, stable semantic tokens for deterministic proof routing."""
    tokens = {
        _EVIDENCE_TOKEN_ALIASES.get(token, token)
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in _EVIDENCE_MATCH_STOPWORDS
    }
    if re.search(r"\bnon[ -]?disclosure\b", value, flags=re.IGNORECASE):
        tokens.add("nda")
    return tokens

_ATOMIC_DOCUMENT_TYPES = {
    "bid_security_and_fees": "emd_or_bid_security",
    "statutory_registrations": "gst_pan_cin_statutory",
    "statutory_registrations_required": "gst_pan_cin_statutory",
    "contractual_declarations": "declaration_or_tender_form",
    "digital_signing": "forms_annexures",
    "financial_capacity": "turnover_net_worth",
    "minimum_industry_experience": "past_work",
    "similar_project_qualification": "past_work",
    "third_party_certification_assurance": "quality_certificate",
    "government_audit_testing_empanelment": "quality_certificate",
    "regulatory_and_statutory_obligations": "statutory_license",
    "delivery_capability_and_infrastructure": "technical_proposal",
    "solution_capabilities": "technical_proposal",
}

_FIELD_EVIDENCE_SPECS: dict[str, list[dict[str, Any]]] = {
    "emd_amount_inr": [
        {
            "document_name": "EMD payment proof or tender-permitted exemption",
            "document_type": "emd_or_bid_security",
            "checks": ["presence", "amount_or_valid_exemption"],
        }
    ],
    "mse_startup_relaxation": [
        {
            "document_name": "Valid Udyam, MSE, or startup eligibility evidence",
            "document_type": "mse_udyam_or_startup_exemption",
            "checks": ["presence", "registration_status", "tender_requirement_match"],
        }
    ],
    "msme_preference_applicable": [
        {
            "document_name": "Valid Udyam or MSE registration",
            "document_type": "mse_udyam_or_startup_exemption",
            "checks": ["presence", "registration_status"],
        },
        {
            "document_name": "Manufacturer, OEM, or service-provider status proof",
            "document_type": "technical_proposal",
            "checks": ["presence", "manufacturer_or_service_provider_status"],
        },
    ],
    "make_in_india_preference": [
        {
            "document_name": "Make in India supplier-class and local-content declaration",
            "document_type": "mii_local_content",
            "checks": ["presence", "supplier_class", "local_content_percent"],
        }
    ],
    "allowed_legal_entities": [
        {
            "document_name": "Bidder legal identity and eligible entity proof",
            "document_type": "gst_pan_cin_statutory",
            "checks": ["presence", "legal_entity_identity"],
        }
    ],
    "statutory_registrations_required": [
        {
            "document_name": "PAN registration evidence",
            "document_type": "gst_pan_cin_statutory",
            "checks": ["presence", "pan"],
        },
        {
            "document_name": "GSTIN registration evidence",
            "document_type": "gst_pan_cin_statutory",
            "checks": ["presence", "gstin"],
        },
    ],
    "non_blacklisting_affidavit": [
        {
            "document_name": "Non-blacklisting or holiday-list declaration",
            "document_type": "integrity_blacklisting_declaration",
            "checks": ["presence", "declaration", "signed"],
        }
    ],
    "local_content_declaration": [
        {
            "document_name": "Local-content percentage and supplier-class declaration",
            "document_type": "mii_local_content",
            "checks": ["presence", "supplier_class", "local_content_percent"],
        },
        {
            "document_name": "Professional local-content certification when the tender value threshold applies",
            "document_type": "mii_local_content",
            "checks": ["conditional_applicability", "professional_certification"],
            "required": False,
        },
    ],
    "integrity_pact_required": [
        {
            "document_name": "Executed Integrity Pact",
            "document_type": "integrity_blacklisting_declaration",
            "checks": ["presence", "signed"],
        }
    ],
    "iso_it_certifications": [
        {
            "document_name": "Required ISO or equivalent quality certification",
            "document_type": "quality_certificate",
            "checks": ["presence", "certificate_standard", "validity"],
        }
    ],
    "permitted_participant_structure": [
        {
            "document_name": "Bidder legal identity and eligible participation structure proof",
            "document_type": "gst_pan_cin_statutory",
            "checks": ["presence", "legal_entity_identity", "participation_structure"],
            "acceptance_rule": (
                "The bidder must match one permitted legal form and must not use a "
                "prohibited JV, consortium, or participation arrangement."
            ),
        }
    ],
    "financial_capacity": [
        {
            "document_name": (
                "Certified audited financial statements or CA / Cost Accountant "
                "turnover certificate"
            ),
            "document_type": "turnover_net_worth",
            "checks": ["presence", "financial_years", "turnover_threshold", "entity_match"],
            "acceptance_rule": (
                "One tender-permitted financial proof must establish the reviewed "
                "threshold for the required period and bidder entity."
            ),
        }
    ],
    "minimum_industry_experience": [
        {
            "document_name": (
                "Relevant contracts, purchase orders, or completion / supply certificates"
            ),
            "document_type": "past_work",
            "checks": ["presence", "experience_period", "relevant_scope", "entity_match"],
            "acceptance_rule": (
                "The submitted past-work evidence must establish the reviewed duration "
                "and relevant scope."
            ),
        }
    ],
    "similar_project_qualification": [
        {
            "document_name": (
                "Tender-permitted work orders, contracts, completion certificates, "
                "service reports, or client references"
            ),
            "document_type": "past_work",
            "checks": ["presence", "project_count", "similarity", "value_or_scale", "lookback_period"],
            "acceptance_rule": (
                "Evidence must satisfy at least one complete alternative route in the "
                "reviewed similar-project requirement."
            ),
        }
    ],
}


__all__ = [
    "_ATOMIC_DOCUMENT_TYPES",
    "_EVIDENCE_MATCH_STOPWORDS",
    "_EVIDENCE_TOKEN_ALIASES",
    "_FIELD_EVIDENCE_SPECS",
    "_evidence_match_tokens",
]
