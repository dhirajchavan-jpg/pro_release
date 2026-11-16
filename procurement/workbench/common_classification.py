"""Bidder document routing, classification projection, and search manifest."""
from __future__ import annotations

import re
from typing import Any

from procurement.domain.classification_routes import (
    PHYSICAL_ROUTE_ALIASES as _PHYSICAL_ROUTE_ALIASES,
    PHYSICAL_ROUTES as _PHYSICAL_ROUTES,
    normalize_physical_route as _normalize_physical_route,
)
from procurement.domain.models import WorkbenchBid


def _route_for_document_type(document_type: str) -> str:
    if document_type in {"emd_or_bid_security", "bid_security_declaration", "bank_guarantee"}:
        return "emd_mse_exemptions"
    if document_type in {
        "past_work_evidence", "experience_po_completion", "oem_authorization",
        "eligibility_pqc",
    }:
        return "pqr_source_approval"
    if document_type in {
        "technical_proposal", "quality_certificate", "datasheet_catalogue",
    }:
        return "technical_quality_scrutiny"
    if document_type in {
        "turnover_networth_ca_certificate", "audited_financials_or_itr", "bank_solvency",
    }:
        return "financial_qualification"
    if document_type in {"boq_or_price_schedule", "financial_bid", "commercial_or_price"}:
        return "commercial_price_restricted"
    if document_type in {"nil_deviation_declaration", "techno_commercial_compliance"}:
        return "techno_commercial_unpriced"
    if document_type == "mse_udyam_or_startup_exemption":
        return "emd_mse_exemptions"
    if document_type in {
        "registration", "registration_document", "poa_or_board_authorization",
        "gst_pan_cin_statutory", "gem_registration", "authorization_document", "consortium_jv",
        "statutory_license", "labour_compliance",
        "environmental_statutory",
    }:
        return "company_statutory_eligibility"
    if document_type in {"", "other", "unknown"}:
        return "needs_review"
    return "declarations_tender_acceptance"


_SECONDARY_ROUTES_BY_TYPE = {
    "mse_udyam_or_startup_exemption": ["company_statutory_eligibility", "techno_commercial_unpriced"],
    "mii_local_content": ["company_statutory_eligibility", "techno_commercial_unpriced"],
    "land_border_declaration": ["company_statutory_eligibility"],
    "non_blacklisting_declaration": ["company_statutory_eligibility"],
    "oem_authorization": ["company_statutory_eligibility"],
    "consortium_jv": ["declarations_tender_acceptance"],
    "statutory_license": ["technical_quality_scrutiny"],
    "labour_compliance": ["technical_quality_scrutiny"],
    "integrity_pact": ["techno_commercial_unpriced"],
    "nil_deviation_declaration": ["declarations_tender_acceptance"],
    "tender_acceptance": ["techno_commercial_unpriced"],
}


_EVIDENCE_PURPOSES_BY_TYPE = {
    "mse_udyam_or_startup_exemption": [
        "MSE status and purchase preference",
        "EMD exemption only when the tender expressly permits it",
    ],
    "emd_or_bid_security": ["EMD or bid-security compliance"],
    "bid_security_declaration": ["Bid-security declaration where the tender permits it"],
    "mii_local_content": ["PPP-MII supplier class and local-content preference"],
    "land_border_declaration": ["Rule 144(xi) land-border declaration or registration"],
    "integrity_pact": ["Pre-bid integrity and authorized acceptance"],
    "integrity_blacklisting_declaration": ["Integrity, debarment, or non-blacklisting compliance"],
    "non_blacklisting_declaration": ["Debarment, holiday-listing, or non-blacklisting compliance"],
    "nil_deviation_declaration": ["Tender-condition acceptance and declared deviations"],
    "techno_commercial_compliance": ["Unpriced techno-commercial and contractual compliance"],
    "tender_acceptance": ["Tender-condition acceptance and unpriced contractual compliance"],
    "quality_certificate": ["Technical quality or certification compliance"],
    "turnover_networth_ca_certificate": ["Financial capacity and threshold evidence"],
    "experience_po_completion": ["Past performance and similar-work experience"],
}


_REFERENCE_SOURCE_DOCUMENT_TYPES = frozenset({
    "tender_notice",
    "rfp_or_bid_document",
    "gem_bid",
    "corrigendum",
    "atc_stc_sla_gtc",
    "technical_specs_or_sow",
    "eligibility_pqc",
    "boq_or_price_schedule",
    "forms_annexures",
    "prebid_clarification",
    "contract_terms",
})


_REFERENCE_EVIDENCE_ROUTE_BY_TYPE = {
    "tpia_report": "technical_quality_scrutiny",
    "third_party_inspection": "technical_quality_scrutiny",
    "third_party_certificate": "technical_quality_scrutiny",
    "inspection_report": "technical_quality_scrutiny",
    "facility_inspection": "technical_quality_scrutiny",
    "quality_certificate": "technical_quality_scrutiny",
    "calibration_certificate": "technical_quality_scrutiny",
    "test_certificate": "technical_quality_scrutiny",
    "client_reference": "pqr_source_approval",
    "client_reference_letter": "pqr_source_approval",
    "past_work_evidence": "pqr_source_approval",
    "experience_po_completion": "pqr_source_approval",
    "completion_certificate": "pqr_source_approval",
}


_REFERENCE_EVIDENCE_PURPOSES_BY_TYPE = {
    "tpia_report": ["Independent third-party inspection and verification"],
    "third_party_inspection": ["Independent third-party inspection and verification"],
    "third_party_certificate": ["Independent third-party certification and scope verification"],
    "inspection_report": ["Independent inspection and verification"],
    "facility_inspection": ["Independent facility or site inspection verification"],
    "quality_certificate": ["Independent certification scope, issuer, and validity verification"],
    "calibration_certificate": ["Independent calibration or test-certificate verification"],
    "test_certificate": ["Independent test or inspection-certificate verification"],
    "client_reference": ["Client reference and prior-performance verification"],
    "client_reference_letter": ["Client reference and prior-performance verification"],
    "past_work_evidence": ["Client-issued past-work and execution evidence"],
    "experience_po_completion": ["Client or issuer past-performance and completion verification"],
    "completion_certificate": ["Client or issuer completion and performance verification"],
}


def _normalized_route_types(values: list[str] | tuple[str, ...]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_"))
    }


def _reference_evidence_route(document_types: list[str], artifact_labels: list[str] | tuple[str, ...]) -> dict[str, Any] | None:
    route_types = _normalized_route_types([*document_types, *artifact_labels])
    if route_types.intersection(_REFERENCE_SOURCE_DOCUMENT_TYPES):
        return None
    matched_types = sorted(
        document_type
        for document_type in route_types
        if document_type in _REFERENCE_EVIDENCE_ROUTE_BY_TYPE
    )
    if not matched_types:
        return None

    routes = {_REFERENCE_EVIDENCE_ROUTE_BY_TYPE[document_type] for document_type in matched_types}
    primary = next(
        route
        for route in ("technical_quality_scrutiny", "pqr_source_approval")
        if route in routes
    )
    purposes: list[str] = []
    for document_type in matched_types:
        for purpose in _REFERENCE_EVIDENCE_PURPOSES_BY_TYPE.get(document_type, []):
            if purpose not in purposes:
                purposes.append(purpose)
    return {
        "broad_route": primary,
        "secondary_routes": sorted(routes - {primary}),
        "evidence_purposes": purposes,
    }


def _route_metadata(
    document_types: list[str],
    content_role: str = "",
    artifact_labels: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    if content_role == "tender_source":
        return {"broad_route": "reference_document", "secondary_routes": [], "evidence_purposes": []}
    if content_role == "reference":
        reference_route = _reference_evidence_route(document_types, artifact_labels)
        if reference_route is not None:
            return reference_route
        return {"broad_route": "reference_document", "secondary_routes": [], "evidence_purposes": []}
    routes = [_route_for_document_type(document_type) for document_type in document_types]
    known_routes = [route for route in routes if route != "needs_review"]
    if not known_routes:
        return {"broad_route": "needs_review", "secondary_routes": [], "evidence_purposes": []}
    non_price = [route for route in known_routes if route != "commercial_price_restricted"]
    primary = (
        non_price[0]
        if non_price
        else "commercial_price_restricted"
        if "commercial_price_restricted" in known_routes
        else "needs_review"
    )
    secondary = []
    for document_type, route in zip(document_types, routes):
        for candidate in [route, *_SECONDARY_ROUTES_BY_TYPE.get(document_type, [])]:
            if candidate not in {primary, "needs_review", "reference_document", "commercial_price_restricted"} and candidate not in secondary:
                secondary.append(candidate)
    purposes = []
    for document_type in document_types:
        for purpose in _EVIDENCE_PURPOSES_BY_TYPE.get(document_type, []):
            if purpose not in purposes:
                purposes.append(purpose)
    return {
        "broad_route": (
            "mixed_submission"
            if "needs_review" in routes or len(set(known_routes)) > 1
            else primary
        ),
        "secondary_routes": secondary,
        "evidence_purposes": purposes,
    }


def _route_for_document_types(document_types: set[str], content_role: str = "") -> str:
    return _route_metadata(sorted(document_types), content_role)["broad_route"]


def _calibrated_document_type(item: dict[str, Any]) -> str:
    document_type = str(item.get("document_type") or "other")
    text = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("content_summary") or ""),
            " ".join(str(value) for value in item.get("key_facts") or []),
        ]
    ).lower()
    if any(marker in text for marker in ("land border with india", "country sharing land border", "rule 144 xi")):
        return "land_border_declaration"
    if document_type == "integrity_blacklisting_declaration":
        if "integrity pact" in text:
            return "integrity_pact"
        if any(marker in text for marker in ("blacklist", "debar", "holiday", "banning")):
            return "non_blacklisting_declaration"
    if any(marker in text for marker in ("nil deviation", "no deviation")):
        return "nil_deviation_declaration"
    return document_type


def _calibrated_content_role(item: dict[str, Any], document_type: str) -> str:
    role = str(item.get("content_role") or "")
    if (
        role == "reference"
        and document_type not in _REFERENCE_SOURCE_DOCUMENT_TYPES
        and document_type not in _REFERENCE_EVIDENCE_ROUTE_BY_TYPE
    ):
        return "vendor_submission"
    return role


def _logical_route_metadata(item: dict[str, Any], *, trust_requested: bool = False) -> dict[str, Any]:
    """Return the route for one logical document, not its containing upload."""
    document_type = _calibrated_document_type(item)
    derived = _route_metadata(
        [document_type],
        _calibrated_content_role(item, document_type),
        tuple(str(label) for label in item.get("labels") or []),
    )
    requested = str(item.get("broad_route") or "").strip()
    route = (
        _normalize_physical_route(requested)
        if requested and (trust_requested or derived["broad_route"] == "needs_review")
        else derived["broad_route"]
    )
    if route == "mixed_submission":
        route = "needs_review"
    secondary = []
    for value in item.get("secondary_routes") or derived.get("secondary_routes") or []:
        normalized = _normalize_physical_route(value)
        if normalized not in {route, "needs_review", "mixed_submission", "reference_document", "commercial_price_restricted"} and normalized not in secondary:
            secondary.append(normalized)
    purposes = list(item.get("evidence_purposes") or derived.get("evidence_purposes") or [])
    reason = str(item.get("route_reason") or item.get("reason") or "").strip()
    if route == "needs_review" and not reason:
        reason = "This logical document needs a document category before approval."
    return {
        "broad_route": route,
        "secondary_routes": secondary,
        "evidence_purposes": purposes,
        "needs_review": route == "needs_review",
        "route_reason": reason,
    }


def _classification_payload(row: WorkbenchBid) -> dict[str, Any]:
    intake = dict(row.intake_json or {})
    classification = dict(intake.get("classification") or {})
    documents = intake.get("documents") or []
    saved = classification.get("reviewed_logical_documents")
    source_logical = classification.get("logical_documents") or []

    def normalize_logical(items: list[dict[str, Any]], *, trust_requested: bool = False) -> list[dict[str, Any]]:
        normalized = []
        for item in items:
            source_ids = item.get("source_document_ids") or [item.get("source_document_id")]
            pages = [
                int(match.group(1))
                for ref in item.get("page_refs") or []
                if (match := re.search(r"(?:p|page[-_]?)(\d+)$", str(ref), re.IGNORECASE))
            ]
            for source_id in filter(None, source_ids):
                page_count = next(
                    (int(doc.get("page_count") or 1) for doc in documents if doc.get("document_id") == source_id),
                    1,
                )
                route = _logical_route_metadata(item, trust_requested=trust_requested)
                logical_id = str(item.get("logical_document_id") or "Bid document")
                if item.get("source_document_ids") or not item.get("source_document_id"):
                    logical_id = f"{logical_id}-{source_id}"
                normalized.append(
                    {
                        "logical_document_id": logical_id,
                        "title": item.get("title") or "Bid document",
                        "document_type": _calibrated_document_type(item),
                        "source_document_id": source_id,
                        "start_page": min(pages) if pages else int(item.get("start_page") or 1),
                        "end_page": max(pages) if pages else int(item.get("end_page") or page_count),
                        "content_summary": item.get("content_summary") or "",
                        "key_facts": item.get("key_facts") or [],
                        "confidence": item.get("confidence") or 0,
                        **route,
                    }
                )
        return normalized

    ai_logical = normalize_logical(source_logical)
    logical = normalize_logical(saved, trust_requested=True) if isinstance(saved, list) else ai_logical
    logical_items_for_metadata = saved if isinstance(saved, list) else source_logical
    route_logical = logical
    routes = classification.get("physical_routes")
    derived_routes = {}
    for document in documents:
        detected = document.get("classification") or {}
        source_id = document.get("document_id")
        document_rows = sorted(
            (item for item in route_logical if item.get("source_document_id") == source_id),
            key=lambda item: (int(item.get("start_page") or 1), int(item.get("end_page") or 1)),
        )
        document_types = [str(item.get("document_type") or "other") for item in document_rows]
        if not document_types:
            document_types = [str(detected.get("document_type") or "other")]
        artifact_labels = [
            str(label)
            for item in logical_items_for_metadata
            if source_id in {
                str(item_source_id)
                for item_source_id in (
                    item.get("source_document_ids") or [item.get("source_document_id")]
                )
            }
            for label in item.get("labels") or []
            if str(label).strip()
        ]
        derived_routes[source_id] = {
            "document_id": source_id,
            **_route_metadata(
                document_types,
                str(detected.get("content_role") or ""),
                artifact_labels,
            ),
        }
    if isinstance(routes, list):
        stored_by_id = {str(item.get("document_id") or ""): item for item in routes}
        normalized_routes = []
        for document_id, derived in derived_routes.items():
            item = stored_by_id.get(str(document_id)) or {}
            stored_route = _normalize_physical_route(item.get("broad_route"))
            broad_route = derived["broad_route"] if stored_route == "needs_review" else stored_route
            normalized_routes.append({
                **item,
                "document_id": document_id,
                "broad_route": broad_route,
                "secondary_routes": [
                    route
                    for route in (
                        _normalize_physical_route(value)
                        for value in item.get("secondary_routes")
                        or derived.get("secondary_routes")
                        or []
                    )
                    if route not in {broad_route, "needs_review", "mixed_submission", "reference_document", "commercial_price_restricted"}
                ],
                "evidence_purposes": item.get("evidence_purposes") or derived.get("evidence_purposes") or [],
                "reason": item.get("reason") or (
                    "This uploaded file contains multiple logical document categories; review the sections below."
                    if broad_route == "mixed_submission"
                    else "This file contains no recognized procurement category yet; classify its document section(s)."
                    if broad_route == "needs_review"
                    else ""
                ),
            })
        routes = normalized_routes
    else:
        routes = [
            {
                **route,
                "reason": (
                    "This file contains no recognized procurement category yet; classify its document section(s)."
                    if route["broad_route"] == "needs_review"
                    else "This uploaded file contains multiple logical document categories; review the sections below."
                    if route["broad_route"] == "mixed_submission"
                    else ""
                ),
            }
            for route in derived_routes.values()
        ]
    return {
        "vendor_id": row.vendor_key,
        "status": "approved" if row.classification_status == "approved" else "draft",
        "tender_match": intake.get("tender_match")
        or {"status": "not_checked", "observed_tender_ids": []},
        "document_types": sorted(
            {str(item.get("document_type") or "other") for item in logical} | {"other"}
        ),
        "classification": {
            "ai_proposal": ai_logical,
            "logical_documents": logical,
            "physical_routes": routes,
        },
    }


def sync_bidder_search_manifest(intake: dict[str, Any]) -> None:
    """Keep the persisted search manifest aligned with the reviewed classification."""
    from procurement.evaluation.index import build_bidder_index_manifest

    classification = intake.get("classification") if isinstance(intake.get("classification"), dict) else {}
    logical = classification.get("reviewed_logical_documents") or classification.get("logical_documents") or []
    normalized = []
    for item in logical:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        source_id = str(row.get("source_document_id") or "")
        if not row.get("page_refs") and source_id:
            start = int(row.get("start_page") or 1)
            end = int(row.get("end_page") or start)
            row["page_refs"] = [f"{source_id}-p{page}" for page in range(start, end + 1)]
        normalized.append(row)
    previous = intake.get("search_index") if isinstance(intake.get("search_index"), dict) else {}
    manifest = build_bidder_index_manifest(normalized, intake.get("pages") or [])
    previous_pages = {
        (str(page.get("page_id") or ""), str(page.get("text_sha256") or ""))
        for page in previous.get("pages") or []
        if isinstance(page, dict)
    }
    current_pages = {
        (str(page.get("page_id") or ""), str(page.get("text_sha256") or ""))
        for page in manifest.get("pages") or []
        if isinstance(page, dict)
    }
    if previous.get("embedding_prewarm") and previous_pages == current_pages:
        manifest["embedding_prewarm"] = previous["embedding_prewarm"]
    intake["search_index"] = manifest
