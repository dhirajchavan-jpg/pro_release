"""Canonical bidder-document routes shared by pipelines and HTTP projections."""
from __future__ import annotations

from typing import Any


PHYSICAL_ROUTE_ALIASES = {
    "company": "company_statutory_eligibility",
    "company_eligibility": "company_statutory_eligibility",
    "security": "emd_mse_exemptions",
    "bid_security_exemption": "emd_mse_exemptions",
    "pqr": "pqr_source_approval",
    "technical": "technical_quality_scrutiny",
    "technical_capability_past_work": "technical_quality_scrutiny",
    "financial": "financial_qualification",
    "declarations": "declarations_tender_acceptance",
    "declarations_tender_forms": "declarations_tender_acceptance",
    "techno_commercial": "techno_commercial_unpriced",
    "commercial_document": "commercial_price_restricted",
    "mixed": "mixed_submission",
}

PHYSICAL_ROUTES = {
    "company_statutory_eligibility",
    "emd_mse_exemptions",
    "pqr_source_approval",
    "technical_quality_scrutiny",
    "financial_qualification",
    "declarations_tender_acceptance",
    "techno_commercial_unpriced",
    "commercial_price_restricted",
    "mixed_submission",
    "reference_document",
    "needs_review",
}


def normalize_physical_route(value: Any) -> str:
    route = PHYSICAL_ROUTE_ALIASES.get(str(value or ""), str(value or ""))
    return route if route in PHYSICAL_ROUTES else "needs_review"
