"""Deterministic bidder-corpus metadata used by every criterion search.

Classification is a routing hint, never an exclusion gate.  The manifest keeps
text and visual coverage explicit while provider-owned vectors remain in the
existing embedding cache instead of being copied into PostgreSQL JSON.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import re
from typing import Any


def _text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).casefold()


_ARTIFACT_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("pan_card", ("pan card", "permanent account number"), ("bidder_identity", "statutory_identifier")),
    ("gst_registration", ("gstin", "gst registration"), ("bidder_identity", "statutory_identifier")),
    ("udyam_certificate", ("udyam", "msme registration", "mse registration"), ("mse_status", "statutory_identifier")),
    ("local_content_certificate", ("local content", "make in india", "class-i local", "class 1 local"), ("local_content_proof",)),
    ("tpia_report", ("tpia", "third party inspection", "bureau veritas", "inspection agency"), ("third_party_verification",)),
    ("facility_inspection", ("plant inspection", "facility inspection", "manufacturing facility", "machinery available"), ("facility_verification",)),
    ("calibration_certificate", ("calibration certificate", "calibration record", "calibrated on"), ("calibration_record",)),
    ("quality_certificate", ("iso 9001", "quality certificate", "quality management system"), ("quality_system",)),
    ("purchase_order", ("purchase order", "work order", "letter of award"), ("experience_award",)),
    ("completion_certificate", ("completion certificate", "performance certificate", "successfully completed"), ("experience_completion",)),
    ("invoice", ("tax invoice", "invoice no", "invoice number", "invoice"), ("execution_proof",)),
    ("financial_statement", ("balance sheet", "profit and loss", "turnover certificate", "net worth"), ("financial_capacity",)),
    ("oem_authorization", ("authorization letter", "authorised channel partner", "authorized channel partner", "authorized to quote", "authorised to quote"), ("oem_authorization", "technical_authorization")),
    ("declaration", ("declaration", "undertaking", "nil deviation"), ("bidder_declaration",)),
    ("technical_checklist", ("technical audit checklist", "quality control checklist"), ("technical_compliance",)),
    (
        "legal_identity",
        (
            "legal identity",
            "legal entity",
            "eligible entity",
            "eligible legal form",
            "participation structure",
            "single legal entity",
            "corporate identity",
            "corporate profile",
            "company profile",
            "company_profile",
            "certificate of incorporation",
            "incorporation certificate",
            "incorporated under",
            "memorandum of association",
            "articles of association",
            "partnership deed",
            "llp agreement",
            "company registration",
            "corporate registration",
            "registered office",
            "gst_pan_cin_statutory",
            "company_incorporation",
            "cin certificate",
            "cin registration",
            "company identification number",
            "corporate identification number",
        ),
        ("bidder_identity",),
    ),
("technical_specification", (
    "technical specification", "technical compliance", "data sheet", "datasheet",
    "offered specification document", "seller specification document",
    "buyer specification document",
), ("technical_compliance",)),
)


def artifact_labels_and_capabilities(
    *,
    document_type: str = "unknown",
    title: str = "",
    summary: str = "",
    key_facts: Iterable[str] = (),
    text: str = "",
) -> tuple[list[str], list[str]]:
    """Return reusable multi-label routing facts without replacing classification."""
    haystack = _text(document_type, title, summary, " ".join(key_facts), text[:12000])
    labels = {str(document_type or "unknown").strip().casefold().replace(" ", "_")}
    capabilities: set[str] = set()
    for label, markers, inferred_capabilities in _ARTIFACT_RULES:
        if any(marker in haystack for marker in markers):
            labels.add(label)
            capabilities.update(inferred_capabilities)
    if any(marker in haystack for marker in ("signed", "signature", "authorised signatory", "authorized signatory")):
        capabilities.add("signature_evidence")
    if any(marker in haystack for marker in ("stamp", "seal", "stamped")):
        capabilities.add("seal_evidence")
    labels.discard("")
    return sorted(labels), sorted(capabilities)


def _normalized_proof_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("-", " ").replace("/", " or ")).strip()


def _allows_declaration_route(text: str) -> bool:
    """Detect an explicit declaration/self-attestation alternative to issuer proof."""
    normalized = _normalized_proof_text(text)
    artifact = r"(?:certificate|certification|license|licence|report|attestation)"
    declaration = r"(?:declaration|undertaking|self\s+attestation|self\s+cert(?:ification)?)"
    return bool(
        re.search(rf"\b{artifact}\b[^.;]{{0,80}}\b(?:or|and\s+or)\b[^.;]{{0,80}}\b{declaration}\b", normalized)
        or re.search(rf"\b{declaration}\b[^.;]{{0,80}}\b(?:or|and\s+or)\b[^.;]{{0,80}}\b{artifact}\b", normalized)
        or re.search(
            rf"\b{declaration}\b[^.;]{{0,100}}\b(?:permitted|allowed|accepted|alternative|instead|in\s+lieu)\b[^.;]{{0,100}}\b{artifact}\b",
            normalized,
        )
        or re.search(
            rf"\b{artifact}\b[^.;]{{0,100}}\b(?:alternative|instead|in\s+lieu)\b[^.;]{{0,100}}\b{declaration}\b",
            normalized,
        )
    )


def _explicit_issuer_only(text: str) -> bool:
    """Keep an issuer gate when the tender explicitly excludes weaker proof."""
    normalized = _normalized_proof_text(text)
    artifact = r"(?:certificate|certification|license|licence|report|attestation)"
    declaration = r"(?:declaration|undertaking|self\s+attestation|self\s+cert(?:ification)?)"
    exclusive = re.search(rf"\bonly\b.{{0,100}}\b{artifact}\b", normalized)
    rejected_declaration = re.search(
        rf"\b{declaration}\b.{{0,80}}\b(?:not\s+sufficient|not\s+accepted|invalid|unacceptable)\b",
        normalized,
    )
    mandatory_artifact = re.search(
        rf"\b{artifact}\b.{{0,80}}\b(?:not\s+sufficient|not\s+accepted|mandatory|compulsory)\b",
        normalized,
    )
    if _allows_declaration_route(normalized):
        return bool(exclusive or rejected_declaration or mandatory_artifact)
    return bool(
        exclusive
        or re.search(rf"\b{artifact}\b.{{0,100}}\b(?:issued\s+by|from|by)\b", normalized)
        or mandatory_artifact
        or rejected_declaration
    )


def infer_proof_contract(evidence: Mapping[str, Any], criterion: Mapping[str, Any]) -> dict[str, Any]:
    """Infer the least rigid proof contract justified by the tender wording."""
    artifact_identity_text = _text(
        evidence.get("document_name"),
        evidence.get("document_type"),
    )
    artifact_text = _text(
        evidence.get("document_name"),
        evidence.get("document_type"),
        evidence.get("acceptance_rule"),
        " ".join(evidence.get("checks") or []),
    )
    extensions = criterion.get("extensions") if isinstance(criterion.get("extensions"), Mapping) else {}
    reviewed = extensions.get("reviewed_field") if isinstance(extensions.get("reviewed_field"), Mapping) else {}
    atomic_text = _text(reviewed.get("atomic_value")) or _text(criterion.get("source_quote"))
    is_generic = not str(evidence.get("document_name") or "").strip() or any(
        marker in artifact_text
        for marker in ("supporting this tender requirement", "bidder evidence for")
    )
    proof_text = _text(artifact_text, atomic_text if is_generic else "")
    labels, capabilities = artifact_labels_and_capabilities(
        document_type=str(evidence.get("document_type") or "unknown"),
        # A generic wrapper describes the criterion, not the submitted
        # artifact.  Do not turn words in that wrapper into proof capabilities.
        title="" if is_generic else str(evidence.get("document_name") or ""),
        # Acceptance rules explain how to judge an artifact; they must not
        # silently reclassify the artifact the tender actually requested.
        text="" if is_generic else artifact_identity_text,
    )
    third_party_markers = ("third party", "tpia", "bureau veritas", "independent inspection")
    is_third_party_wrapper = (
        any(marker in proof_text for marker in third_party_markers)
        and any(marker in artifact_identity_text for marker in ("report", "certificate", "certification", "letterhead"))
    )
    if is_third_party_wrapper:
        # A verification report proves that an independent party performed the
        # stated check.  References inside that report to invoices, financials,
        # or registrations do not turn the report itself into every underlying
        # primary artifact.  Facility-focused reports additionally prove the
        # inspected facility scope.
        capabilities = ["third_party_verification"]
        if any(marker in proof_text for marker in ("plant", "facility", "machinery", "manufacturing")):
            capabilities.append("facility_verification")
    declaration_route = _allows_declaration_route(proof_text) and not _explicit_issuer_only(proof_text)
    if declaration_route:
        minimum_strength = "bidder_declaration"
        issuer_roles = []
    elif any(marker in proof_text for marker in third_party_markers):
        minimum_strength = "third_party_verified"
        issuer_roles = ["tpia"]
    elif any(marker in proof_text for marker in ("certificate", "registration", "licence", "license", "consent", "purchase order", "completion", "invoice")):
        minimum_strength = "issuer_certificate"
        issuer_roles = ["issuer"]
    elif any(marker in proof_text for marker in ("declaration", "undertaking", "form", "self cert")):
        minimum_strength = "bidder_declaration"
        issuer_roles = []
    else:
        minimum_strength = "claim"
        issuer_roles = []

    alternative_capabilities: list[list[str]] = []
    has_artifact_alternative = (
        " or " in f" {artifact_text} "
        or " either " in f" {artifact_text} "
        or " / " in artifact_text
    )
    if has_artifact_alternative and len(capabilities) > 1:
        alternative_capabilities = [sorted(capabilities)]
        capabilities = []
        if any(marker in artifact_text for marker in ("declaration", "undertaking")):
            minimum_strength = "bidder_declaration"
            issuer_roles = []

    if any(marker in proof_text for marker in ("plant", "facility", "machinery", "calibration", "manufacturing")):
        entity_roles = ["plant"]
    else:
        entity_roles = ["bidder"]
    visual_checks = sorted({
        check
        for check in ("signature", "seal", "stamp", "letterhead", "photograph", "visual_layout")
        if check in artifact_text
    })
    return {
        "minimum_strength": minimum_strength,
        "accepted_artifact_labels": labels,
        "required_capabilities": capabilities,
        "required_capability_groups": alternative_capabilities,
        "entity_roles": entity_roles,
        "issuer_roles": issuer_roles,
        "visual_checks": visual_checks,
    }


def build_bidder_index_manifest(logical_documents: Iterable[Any], pages: Iterable[Any]) -> dict[str, Any]:
    """Build a small persistent manifest; vectors stay in the existing cache."""
    page_rows: list[dict[str, Any]] = []
    for page in pages:
        get = page.get if isinstance(page, Mapping) else lambda key, default=None: getattr(page, key, default)
        page_text = str(get("text", "") or "")
        blocks = get("blocks", []) or []
        tables = get("tables", []) or []
        page_rows.append({
            "page_id": str(get("page_id", "") or ""),
            "document_id": str(get("document_id", "") or ""),
            "page_number": int(get("page_number", 0) or 0),
            "text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
            "text_chars": len(page_text),
            "has_layout": bool(blocks or tables),
            "has_tables": bool(tables),
            "visual_available": True,
            "ocr_required": bool(get("ocr_required", False)),
        })
    document_rows: list[dict[str, Any]] = []
    for document in logical_documents:
        get = document.get if isinstance(document, Mapping) else lambda key, default=None: getattr(document, key, default)
        labels, capabilities = artifact_labels_and_capabilities(
            document_type=str(get("document_type", "unknown") or "unknown"),
            title=str(get("title", "") or ""),
            summary=str(get("content_summary", "") or ""),
            key_facts=get("key_facts", []) or [],
        )
        document_rows.append({
            "logical_document_id": str(get("logical_document_id", "") or ""),
            "document_type": str(get("document_type", "unknown") or "unknown"),
            "content_role": str(get("content_role", "unknown") or "unknown"),
            "page_ids": list(get("page_refs", []) or []),
            "labels": labels,
            "capabilities": capabilities,
        })
    payload = {"documents": document_rows, "pages": page_rows}
    corpus_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "bidder-search-index.v1",
        "corpus_sha256": corpus_hash,
        "embedding_cache_key": f"bidder:{corpus_hash}",
        "documents": document_rows,
        "pages": page_rows,
        "coverage": {
            "logical_documents": len(document_rows),
            "pages": len(page_rows),
            "readable_pages": sum(row["text_chars"] > 0 for row in page_rows),
            "visual_pages": sum(row["visual_available"] for row in page_rows),
            "table_pages": sum(row["has_tables"] for row in page_rows),
        },
    }


__all__ = [
    "artifact_labels_and_capabilities",
    "build_bidder_index_manifest",
    "infer_proof_contract",
]
