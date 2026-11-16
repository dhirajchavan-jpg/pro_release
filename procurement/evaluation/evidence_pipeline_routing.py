"""Source-aware candidate routing for bid evidence."""

from __future__ import annotations

import os
import re
from typing import Any

from procurement.domain.document_dictionary import procurement_dictionary_entries_for_text
from procurement.domain.schemas import EvidenceRequirement, ExtractedDocument, LogicalDocument, PageUnit, RequirementMatrixItem
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match
from procurement.evaluation.criteria import criterion_atomic_requirement_text
from procurement.evaluation.facts import canonical_evidence_type
from procurement.understanding.classification import bidder_filled_evidence_marker, reference_source_material, tender_source_trap


BID_EVIDENCE_ROUTE_MAX_PAGES = max(1, int(os.getenv("BID_EVIDENCE_ROUTE_MAX_PAGES", "6")))
EVIDENCE_VERIFIER_PAGE_CHARS = max(500, int(os.getenv("EVIDENCE_VERIFIER_PAGE_CHARS", "2500")))


def _annexure_labels(value: str) -> set[str]:
    labels: set[str] = set()
    for match in re.finditer(r"annexure\s*[-:]?\s*((?:\d+[a-z]?\s*){1,2})", value, flags=re.IGNORECASE):
        labels.update(re.findall(r"\d+[a-z]?", match.group(1).casefold()))
    return labels


def applicability_signal_score(item: RequirementMatrixItem, text: str) -> int:
    applicability = item.applicability if isinstance(item.applicability, dict) else {}
    if applicability.get("state") != "conditional":
        return 0
    trigger = " ".join([
        str(applicability.get("rule") or ""),
        " ".join(str(value) for value in applicability.get("conditions") or []),
    ])
    terms = keyword_tokens(trigger) - {
        "applicable", "bidder", "condition", "if", "is", "not", "only", "required", "shall", "the", "when",
    }
    overlap = terms & keyword_tokens(text)
    threshold = min(2, len(terms))
    return 10 if threshold and len(overlap) >= threshold else 0


def _hook(hooks: dict[str, Any] | None, name: str, default: Any) -> Any:
    return default if hooks is None else hooks.get(name, default)


def specific_evidence_mismatch(evidence: EvidenceRequirement, text: str) -> bool:
    haystack = text.lower()
    name = evidence.document_name.lower()
    document_type = canonical_evidence_type(evidence.document_type)
    checks = {check.casefold() for check in evidence.checks}
    expected_annexures = _annexure_labels(evidence.document_name)
    actual_annexures = _annexure_labels(text)
    if expected_annexures and actual_annexures and expected_annexures.isdisjoint(actual_annexures):
        return True
    if "manufacturer_or_service_provider_status" in checks and not any(
        marker in haystack
        for marker in (
            "manufacturer of the quoted",
            "manufacturer of items quoted",
            "mse manufacturer",
            "service provider for quoted",
        )
    ):
        return True
    if "pan" in checks and not re.search(
        r"(?:\bpan\s*[:\-]?\s*)?([a-z]{5}\d{4}[a-z])\b",
        haystack,
    ):
        return True
    if "gstin" in checks and not any(marker in haystack for marker in ("gstin", "gst registration", "do you have gstin")):
        return True
    required_markers = {
        "emd_or_bid_security": [
            "emd",
            "earnest money",
            "bid security",
            "bank guarantee",
            "utr",
            "udyam registration certificate",
            "startup certificate",
        ],
        "mse_udyam_or_startup_exemption": [
            "udyam",
            "msme",
            "micro enterprise",
            "small enterprise",
            "startup recognition",
        ],
        "quality_certificate": ["iso ", "iso-", "quality management", "certificate no"],
        "gst_pan_cin_statutory": ["pan", "gstin", "gst registration", "cin", "udyam registration certificate"],
        "mii_local_content": ["local content", "supplier class", "class-i", "class i", "class-ii", "class ii", "make in india"],
    }
    markers = required_markers.get(document_type)
    if markers and not any(marker in haystack for marker in markers):
        return True
    if any(term in name for term in ["purchase order", "work order", "loi", "letter of intent"]):
        return not any(term in haystack for term in ["purchase order", "work order", "letter of intent", "loi", "award of work"])
    if any(term in name for term in ["completion", "final bill", "invoice", "execution"]):
        return not any(
            term in haystack
            for term in [
                "completion certificate",
                "work completion",
                "completed",
                "executed value",
                "final bill",
                "tax invoice",
                "invoice",
                "service entry sheet",
                "ses",
                "measurement book",
                "ra bill",
                "running account bill",
                "payment advice",
            ]
        )
    if "nil deviation" in name or ("deviation" in name and "blacklist" not in name and "holiday" not in name):
        return "nil deviation" not in haystack
    if any(term in name for term in ["blacklisting", "blacklist", "holiday", "debar", "integrity"]):
        return not any(term in haystack for term in ["blacklisting", "blacklisted", "holiday", "debar", "integrity"])
    if "local content" in name:
        return not any(term in haystack for term in ["local content", "supplier class", "class-i", "class ii", "make in india"])
    if document_type == "oem_authorization" or "oem authorization" in name:
        return not any(term in haystack for term in ["oem authorization", "authorization letter", "authorized bidder", "authorised bidder"])
    return False


def evidence_marker_score(evidence: EvidenceRequirement, text: str) -> int:
    haystack = text.lower()
    normalized_haystack = normalize_for_match(text)
    haystack_tokens = keyword_tokens(normalized_haystack)

    def contains_marker(value: str) -> bool:
        marker = normalize_for_match(value)
        if not marker:
            return False
        return marker in haystack_tokens if len(marker) <= 5 and " " not in marker else marker in normalized_haystack
    doc_type = canonical_evidence_type(evidence.document_type)
    name = f"{evidence.document_name} {doc_type}".lower()
    markers = {
        "emd_or_bid_security": ["emd", "earnest money", "bid security", "utr", "payment receipt", "bank guarantee"],
        "mse_udyam_or_startup_exemption": ["udyam registration certificate", "udyam number", "enterprise name", "msme"],
        "experience_po_completion": [
            "purchase order",
            "work order",
            "po date",
            "completion certificate",
            "work completion",
            "tax invoice",
            "invoice",
            "final bill",
            "service entry sheet",
            "executed value",
        ],
        "oem_authorization": ["oem authorization", "authorization letter", "authorized bidder", "authorised bidder"],
        "quality_certificate": ["iso", "quality management", "certificate"],
        "calibration_certificate": [
            "calibration certificate", "calibration record", "calibrated on",
            "calibration date", "instrument id", "equipment id",
        ],
        "integrity_blacklisting_declaration": ["blacklist", "blacklisted", "holiday", "debarred", "integrity pact"],
        "mii_local_content": ["local content", "class-i", "class ii", "make in india"],
        "gst_pan_cin_statutory": ["gstin", "pan", "cin"],
        "environmental_statutory": [
            "air act", "water act", "pollution control", "environmental clearance",
            "environment clearance", "consent to operate", "consent to establish",
        ],
        "statutory_license": ["licence", "license", "registration", "statutory consent"],
        "labour_compliance": ["epf", "esic", "provident fund", "labour licence", "labour license"],
        "turnover_networth_ca_certificate": ["turnover", "net worth", "ca certificate", "balance sheet"],
    }
    score = 0
    expected_annexures = _annexure_labels(evidence.document_name)
    if expected_annexures & _annexure_labels(text):
        score += 12
    for entry in procurement_dictionary_entries_for_text(name):
        terms = [
            str(entry.get("display_name", "")),
            str(entry.get("canonical_key", "")).replace("_", " "),
            *[str(alias) for alias in entry.get("aliases", []) if str(alias).strip()],
        ]
        score += sum(2 for term in terms if contains_marker(term))
    for key, values in markers.items():
        if key in name or doc_type == key:
            score += sum(2 for value in values if contains_marker(value))
    if "declaration" in name:
        score += sum(2 for value in ["declaration", "undertaking", "we declare", "we confirm"] if value in haystack)
    return score


def vendor_content_role_allowed(role: str) -> bool:
    # All callers already operate on bidder uploads.  A weak "reference" label
    # must not hide a genuine TPIA/certificate page; copied tender text is
    # rejected separately by tender_source_trap and evidence verification.
    return role != "tender_source"


def strong_vendor_evidence_marker(text: str) -> bool:
    haystack = normalize_for_match(text)
    if reference_source_material(haystack) and not bidder_filled_evidence_marker(haystack):
        return False
    return any(
        marker in haystack
        for marker in [
            "purchase order no",
            "po no",
            "work order no",
            "letter of intent",
            "loi",
            "award of work",
            "completion certificate",
            "final bill",
            "tax invoice",
            "invoice no",
            "service entry sheet",
            "executed value",
            "udyam registration certificate",
            "udyam number",
            "udin",
            "chartered accountant",
            "turnover",
            "balance sheet",
            "local content",
            "class-i",
            "class ii",
            "emd",
            "bank guarantee",
        ]
    )


def source_like_document_type(document_type: str) -> bool:
    return document_type in {
        "tender_notice",
        "rfp_or_bid_document",
        "gem_bid",
        "corrigendum",
        "atc_stc_sla_gtc",
        "prebid_clarification",
        "contract_terms",
    }


def document_type_compatible(
    expected: str,
    actual: str,
    text: str = "",
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> bool:
    expected = canonical_evidence_type(expected)
    actual = canonical_evidence_type(actual)
    if expected in {"unknown", "other"} or actual in {"unknown", "other"}:
        marker_score = _hook(compatibility_hooks, "evidence_marker_score", evidence_marker_score)
        return marker_score(EvidenceRequirement(evidence_id="", document_name=expected, document_type=expected), text) > 0
    if expected == actual:
        return True
    compatible = {
        "forms_annexures": {"integrity_blacklisting_declaration", "mii_local_content", "technical_proposal"},
        "integrity_blacklisting_declaration": {"forms_annexures"},
        "mii_local_content": {"forms_annexures"},
        "technical_proposal": {"datasheet_catalogue", "technical_specs_or_sow", "forms_annexures"},
        "environmental_statutory": {"statutory_license"},
        "statutory_license": {"environmental_statutory", "labour_compliance"},
    }
    return actual in compatible.get(expected, set())


def match_logical_documents(
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    vendor_logical_docs: list[LogicalDocument],
    doc_text_by_logical_id: dict[str, str],
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> list[LogicalDocument]:
    role_allowed = _hook(compatibility_hooks, "vendor_content_role_allowed", vendor_content_role_allowed)
    routing_text = _hook(compatibility_hooks, "logical_document_routing_text", logical_document_routing_text)
    mismatch = _hook(compatibility_hooks, "specific_evidence_mismatch", specific_evidence_mismatch)
    source_like = _hook(compatibility_hooks, "source_like_document_type", source_like_document_type)
    strong_marker = _hook(compatibility_hooks, "strong_vendor_evidence_marker", strong_vendor_evidence_marker)
    type_compatible = _hook(compatibility_hooks, "document_type_compatible", document_type_compatible)
    scored: list[tuple[int, LogicalDocument]] = []
    query = criterion_evidence_query(item, evidence)
    query_tokens = keyword_tokens(query)
    for logical_doc in vendor_logical_docs:
        if not role_allowed(logical_doc.content_role):
            continue
        content = doc_text_by_logical_id.get(logical_doc.logical_document_id, "")[:3000]
        haystack = routing_text(logical_doc, content)
        haystack_tokens = keyword_tokens(haystack)
        score = len(query_tokens & haystack_tokens)
        applicability_score = applicability_signal_score(item, haystack)
        if mismatch(evidence, haystack) and not applicability_score:
            score -= 4
        if source_like(logical_doc.document_type) and not strong_marker(content):
            score -= 6
        if tender_source_trap(haystack.lower()):
            score -= 6
        if evidence.document_type != "unknown" and evidence.document_type == logical_doc.document_type:
            score += 6
        elif evidence.document_type != "unknown" and not type_compatible(evidence.document_type, logical_doc.document_type, haystack):
            score -= 4
        if evidence.document_type != "unknown" and evidence.document_type in haystack.lower():
            score += 3
        if score > 0:
            scored.append((score, logical_doc))
    scored.sort(key=lambda item_: item_[0], reverse=True)
    return [doc for score, doc in scored[:4] if score >= 2 or evidence.document_type == doc.document_type]


def vendor_page_candidates(vendor_logical_docs: list[LogicalDocument], docs: list[ExtractedDocument]) -> list[dict[str, Any]]:
    pages_by_id = {
        page.page_id: page
        for document in docs
        for page in document.pages
    }
    candidates: list[dict[str, Any]] = []
    for logical_doc in vendor_logical_docs:
        for page_id in logical_doc.page_refs:
            page = pages_by_id.get(page_id)
            if not page:
                continue
            candidates.append({"logical_doc": logical_doc, "page": page, "text": page.text or ""})
    return candidates


def logical_docs_for_page_ids(
    page_ids: list[str],
    vendor_logical_docs: list[LogicalDocument],
) -> list[LogicalDocument]:
    wanted = set(page_ids)
    return [
        logical_doc
        for logical_doc in vendor_logical_docs
        if any(page_id in wanted for page_id in logical_doc.page_refs)
    ]


def match_evidence_pages(
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    candidates: list[dict[str, Any]],
    *,
    max_pages: int = BID_EVIDENCE_ROUTE_MAX_PAGES,
    compatibility_hooks: dict[str, Any] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    role_allowed = _hook(compatibility_hooks, "vendor_content_role_allowed", vendor_content_role_allowed)
    mismatch = _hook(compatibility_hooks, "specific_evidence_mismatch", specific_evidence_mismatch)
    source_like = _hook(compatibility_hooks, "source_like_document_type", source_like_document_type)
    strong_marker = _hook(compatibility_hooks, "strong_vendor_evidence_marker", strong_vendor_evidence_marker)
    type_compatible = _hook(compatibility_hooks, "document_type_compatible", document_type_compatible)
    marker_score = _hook(compatibility_hooks, "evidence_marker_score", evidence_marker_score)
    query = criterion_evidence_query(item, evidence)
    query_tokens = keyword_tokens(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for candidate in candidates:
        logical_doc: LogicalDocument = candidate["logical_doc"]
        page: PageUnit = candidate["page"]
        if not page.text:
            continue
        page_role = page.classification.content_role if page.classification else logical_doc.content_role
        page_type = page.classification.document_type if page.classification else logical_doc.document_type
        if not role_allowed(page_role):
            continue
        haystack = " ".join([logical_doc.document_type, page_type, page.text[:3000]])
        applicability_score = applicability_signal_score(item, haystack)
        score = len(query_tokens & keyword_tokens(haystack))
        if mismatch(evidence, haystack) and not applicability_score:
            score -= 4
        if source_like(logical_doc.document_type) and not strong_marker(page.text):
            score -= 6
        if tender_source_trap(page.text.lower()):
            score -= 6
        if evidence.document_type != "unknown" and type_compatible(evidence.document_type, page_type, haystack):
            score += 8
        elif evidence.document_type != "unknown" and type_compatible(evidence.document_type, logical_doc.document_type, haystack):
            score += 5
        elif evidence.document_type != "unknown" and not applicability_score:
            score -= 3
        score += marker_score(evidence, haystack)
        score += applicability_score
        if score >= 4:
            scored.append((score, candidate))
    scored.sort(key=lambda item_: item_[0], reverse=True)
    return scored[:max_pages]


def logical_docs_for_page_matches(
    page_matches: list[tuple[int, dict[str, Any]]],
    vendor_logical_docs: list[LogicalDocument],
) -> list[LogicalDocument]:
    by_id = {doc.logical_document_id: doc for doc in vendor_logical_docs}
    result: list[LogicalDocument] = []
    seen: set[str] = set()
    for _, candidate in page_matches:
        logical_id = candidate["logical_doc"].logical_document_id
        if logical_id not in seen and logical_id in by_id:
            seen.add(logical_id)
            result.append(by_id[logical_id])
    return result


def logical_document_text_index(logical_documents: list[LogicalDocument], docs: list[ExtractedDocument]) -> dict[str, str]:
    pages_by_id = {
        page.page_id: page
        for document in docs
        for page in document.pages
    }
    text_by_id: dict[str, str] = {}
    for logical_doc in logical_documents:
        parts = [
            pages_by_id[page_id].text
            for page_id in logical_doc.page_refs
            if page_id in pages_by_id
        ]
        if not parts:
            source_ids = set(logical_doc.source_document_ids)
            parts = [document.text for document in docs if document.record.document_id in source_ids]
        text_by_id[logical_doc.logical_document_id] = clean_text("\n".join(parts))
    return text_by_id


def logical_document_routing_text(logical_doc: LogicalDocument, content: str = "") -> str:
    return " ".join(
        [
            logical_doc.title,
            logical_doc.document_type,
            logical_doc.content_summary,
            " ".join(logical_doc.key_facts),
            content,
        ]
    )


def logical_document_summary_score(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    logical_doc: LogicalDocument,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> int:
    if not logical_doc.content_summary and not logical_doc.key_facts:
        return 0
    routing_text = _hook(compatibility_hooks, "logical_document_routing_text", logical_document_routing_text)
    evidence_query = " ".join(
        [
            evidence.document_name,
            evidence.document_type,
            " ".join(evidence.checks),
            evidence.acceptance_rule,
        ]
    )
    query = evidence_query
    if "supporting this tender requirement" in evidence.document_name.lower():
        query = f"{evidence_query} {item.requirement} {item.pass_rule}"
    return len(
        keyword_tokens(query)
        & keyword_tokens(routing_text(logical_doc))
    )


def criterion_evidence_query(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
) -> str:
    applicability = item.applicability if isinstance(item.applicability, dict) else {}
    return " ".join(
        [
            criterion_atomic_requirement_text(item),
            item.pass_rule,
            str(applicability.get("rule") or ""),
            " ".join(str(value) for value in applicability.get("conditions") or []),
            evidence.document_name,
            evidence.document_type,
            " ".join(evidence.checks),
            evidence.acceptance_rule,
        ]
    )


def criterion_page_excerpt(
    page: PageUnit,
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    *,
    verifier_page_chars: int = EVIDENCE_VERIFIER_PAGE_CHARS,
    compatibility_hooks: dict[str, Any] | None = None,
) -> str:
    """Return the relevant part of a long page instead of always taking its beginning."""
    text = clean_text(page.text)
    if len(text) <= verifier_page_chars:
        return text
    evidence_query = _hook(compatibility_hooks, "criterion_evidence_query", criterion_evidence_query)
    terms = sorted(keyword_tokens(evidence_query(item, evidence)), key=len, reverse=True)
    normalized = text.lower()
    positions = [normalized.find(term) for term in terms if len(term) >= 3 and normalized.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - verifier_page_chars // 3)
    return text[start : start + verifier_page_chars]


__all__ = [
    "BID_EVIDENCE_ROUTE_MAX_PAGES",
    "EVIDENCE_VERIFIER_PAGE_CHARS",
    "specific_evidence_mismatch",
    "evidence_marker_score",
    "vendor_content_role_allowed",
    "strong_vendor_evidence_marker",
    "source_like_document_type",
    "document_type_compatible",
    "match_logical_documents",
    "vendor_page_candidates",
    "logical_docs_for_page_ids",
    "match_evidence_pages",
    "logical_docs_for_page_matches",
    "logical_document_text_index",
    "logical_document_routing_text",
    "logical_document_summary_score",
    "applicability_signal_score",
    "criterion_evidence_query",
    "criterion_page_excerpt",
]
