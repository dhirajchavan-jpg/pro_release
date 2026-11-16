"""Annexure discovery and tender-packet backfill rules."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.document_dictionary import procurement_dictionary_entries_for_text
from procurement.domain.schemas import (
    ExtractedDocument,
    PageUnit,
    TenderEvidenceDocument,
    TenderExtraction,
    TenderRequiredDocument,
    TenderRequirementExtraction,
)
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text, normalize_for_match
from procurement.understanding.classification import (
    page_has_bidder_submission_signal,
    page_has_mandatory_submission_signal,
)
from procurement.understanding.requirements import requirement_document_keys
from .extraction_normalization import (
    normalize_extraction_document,
    normalize_required_extraction_document,
    normalize_tender_extraction_data,
    procurement_dictionary_entry_by_key,
    tender_source_documents,
)
from .extraction_requirements import (
    BQC_SPECIFIC_DOCUMENT_KEYS,
    document_key_needs_requirement_owner,
    is_keepable_tender_requirement,
    merge_requirement_row,
    merge_tender_dict,
)

def merge_tender_extraction_dicts(partials: list[dict[str, Any]], tender_docs: list[ExtractedDocument]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "schema_version": "tender_extraction_v1",
        "source_stage": "runtime",
        "extraction_status": "complete",
        "source_documents": tender_source_documents(tender_docs),
        "tender": {},
        "annexures": [],
        "requirements": [],
        "required_documents": [],
        "tender_packet_gaps": {"missing_referenced_attachments": [], "not_extractable_fields": []},
        "audit": {"derived_by": "tender_extraction_chunk_merge", "chunk_count": len(partials)},
    }
    annexures_by_key: dict[str, dict[str, Any]] = {}
    merged_requirements: list[dict[str, Any]] = []
    required_by_key: dict[str, dict[str, Any]] = {}
    gap_keys: set[str] = set()
    field_keys: set[str] = set()
    statuses: set[str] = set()

    for partial in partials:
        normalize_tender_extraction_data(partial, tender_docs)
        if partial.get("extraction_status"):
            statuses.add(str(partial.get("extraction_status")))
        merge_tender_dict(merged["tender"], partial.get("tender") or {})
        for annexure in partial.get("annexures") or []:
            if not isinstance(annexure, dict):
                continue
            key = annexure_identity_key(annexure)
            existing = annexures_by_key.setdefault(key, annexure)
            existing["canonical_document_keys"] = sorted(
                {
                    str(value)
                    for value in (existing.get("canonical_document_keys") or []) + (annexure.get("canonical_document_keys") or [])
                    if str(value).strip()
                }
            )
        for requirement in partial.get("requirements") or []:
            if not isinstance(requirement, dict):
                continue
            if not is_keepable_tender_requirement(requirement):
                continue
            merge_requirement_row(merged_requirements, requirement)
        for document in partial.get("required_documents") or []:
            if not isinstance(document, dict):
                continue
            key = str(document.get("canonical_key") or "").strip()
            if not key:
                continue
            existing = required_by_key.setdefault(key, document)
            for field in ("required_for_requirement_ids", "source_annexures", "checks"):
                existing[field] = sorted(
                    {
                        str(value)
                        for value in (existing.get(field) or []) + (document.get(field) or [])
                        if str(value).strip()
                    }
                )
        gaps = (partial.get("tender_packet_gaps") or {}).get("missing_referenced_attachments") or []
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            key = normalize_for_match(str(gap.get("attachment") or ""))
            if key and key not in gap_keys:
                merged["tender_packet_gaps"]["missing_referenced_attachments"].append(gap)
                gap_keys.add(key)
        fields = (partial.get("tender_packet_gaps") or {}).get("not_extractable_fields") or []
        for field in fields:
            if not isinstance(field, dict):
                continue
            key = normalize_for_match(str(field.get("field") or ""))
            if key and key not in field_keys:
                merged["tender_packet_gaps"]["not_extractable_fields"].append(field)
                field_keys.add(key)

    merged["annexures"] = list(annexures_by_key.values())
    merged["requirements"] = merged_requirements
    for index, requirement in enumerate(merged["requirements"], start=1):
        requirement["requirement_id"] = f"T1-C{index}"
    merged["required_documents"] = list(required_by_key.values())
    if "insufficient_source" in statuses:
        merged["extraction_status"] = "insufficient_source"
    elif merged["tender_packet_gaps"]["missing_referenced_attachments"]:
        merged["extraction_status"] = "partial_referenced_attachments_missing"
    return merged

def annexure_identity_key(item: dict[str, Any]) -> str:
    annexure_no = normalize_annexure_no(item.get("annexure_no"))
    title = normalize_for_match(str(item.get("title") or ""))[:80]
    return f"{annexure_no}:{title}"


def normalize_annexure_no(value: Any) -> str:
    text = str(value or "").upper().strip()
    text = re.sub(r"^ANNEXURE\s+", "", text)
    text = text.strip(" .:-")
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[^A-Z0-9-]+", "", text)


ROMAN_ANNEXURE_PATTERN = re.compile(
    r"(?P<number>\b(?:XIV|XIII|XII|XI|XV|VIII|VII|VI|IV|III|II|IX|X|V|I)\.)\s+"
    r"(?P<title>.*?)(?=\s+(?:XIV|XIII|XII|XI|XV|VIII|VII|VI|IV|III|II|IX|X|V|I)\.\s+|\n\s*\d+\.\s+Tender details|\Z)",
    re.IGNORECASE | re.DOTALL,
)


ANNEXURE_LINE_PATTERN = re.compile(
    r"(?:\b\d+(?:\.\d+)*\.?\s*)?ANNEXURE\s+"
    r"(?P<number>(?:\d+\s*\d?\s*[A-Z]?)|[A-Z]|[IVXLCDM]+)"
    r"\s*(?:[-:.]\s*|\s+:\s*|\s+)"
    r"(?P<title>.+)",
    re.IGNORECASE,
)


ROMAN_ANNEXURE_LINE_PATTERN = re.compile(
    r"^\s*(?P<number>XIV|XIII|XII|XI|XV|VIII|VII|VI|IV|III|II|IX|X|V|I)\.\s+(?P<title>.+)$",
    re.IGNORECASE,
)


PORTAL_SUBMISSION_CONTROL_KEYS = {
    "bid_submission_form",
    "technical_commercial_compliance",
    "financial_bid_boq",
    "tender_acceptance",
}


ANNEXURE_REQUIRED_DOCUMENT_KEYS = {
    "bqc_qualification_documents",
    "technical_spec_compliance",
    "drawings",
    "deviation_statement",
    "billing_address",
    "integrity_pact",
    "land_border_declaration",
    "social_media_policy",
    "tpi_certificate",
    "site_visit_scope_understanding",
    "local_content_declaration",
}


def backfill_tender_annexures(extraction: TenderExtraction, tender_docs: list[ExtractedDocument]) -> dict[str, dict[str, Any]]:
    parsed = extract_listed_annexures(tender_docs)
    existing_by_key = {annexure_identity_key(item): item for item in extraction.annexures if isinstance(item, dict)}
    for identity, row in parsed.items():
        existing = existing_by_key.get(identity)
        if existing:
            existing.setdefault("title", row["title"])
            existing.setdefault("presence_in_uploaded_pdf", row["presence_in_uploaded_pdf"])
            existing.setdefault("document_role", row["document_role"])
            existing.setdefault("requires_bidder_submission", row["requires_bidder_submission"])
            existing.setdefault("source_ref", row["source_ref"])
            existing["canonical_document_keys"] = sorted(
                {str(key) for key in existing.get("canonical_document_keys", []) + row["canonical_document_keys"] if str(key).strip()}
            )
        else:
            extraction.annexures.append(row)
            existing_by_key[identity] = row
    return existing_by_key


def extract_listed_annexures(tender_docs: list[ExtractedDocument]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for document in tender_docs:
        for page in document.pages:
            page_has_annexure_context = "annexure" in normalize_for_match(page.text)
            for line in page.text.splitlines():
                for pattern in (ANNEXURE_LINE_PATTERN, ROMAN_ANNEXURE_LINE_PATTERN):
                    match = pattern.search(line)
                    if not match:
                        continue
                    if pattern is ROMAN_ANNEXURE_LINE_PATTERN and not page_has_annexure_context:
                        continue
                    annexure_no = normalize_annexure_no(match.group("number"))
                    title = clean_annexure_title(match.group("title"))
                    if pattern is ROMAN_ANNEXURE_LINE_PATTERN and not is_plausible_roman_annexure_title(title):
                        continue
                    if not annexure_no or not title or len(title) > 220:
                        continue
                    row = annexure_row(page, annexure_no, title, line)
                    rows.setdefault(annexure_identity_key(row), row)
            for match in ROMAN_ANNEXURE_PATTERN.finditer(page.text):
                annexure_no = normalize_annexure_no(match.group("number"))
                title = clean_annexure_title(match.group("title"))
                if not is_plausible_roman_annexure_title(title):
                    continue
                if not annexure_no or not title or len(title) > 220:
                    continue
                row = annexure_row(page, annexure_no, title, match.group("title"))
                rows.setdefault(annexure_identity_key(row), row)
    return rows


def annexure_row(page: PageUnit, annexure_no: str, title: str, source_text: str = "") -> dict[str, Any]:
    quote = f"Annexure {annexure_no}: {title}"
    return {
        "annexure_no": annexure_no,
        "title": title,
        "presence_in_uploaded_pdf": "listed_only",
        "document_role": infer_annexure_document_role(title, annexure_no, source_text),
        "canonical_document_keys": annexure_document_keys(title, annexure_no, source_text),
        "requires_bidder_submission": infer_annexure_submission(title, annexure_no, source_text),
        "source_ref": {"document_id": page.document_id, "page_number": page.page_number, "quote": quote[:240]},
    }


def clean_annexure_title(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*-{2,}\s*(?:\d{1,3})?\s*$", "", text)
    text = re.sub(r"\s+(?:page\s*)?\d{1,3}\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .:-")
    return text


def is_plausible_roman_annexure_title(title: str) -> bool:
    normalized = normalize_for_match(title)
    if not normalized:
        return False
    if len(normalized.split()) > 16 and not procurement_dictionary_entries_for_text(title):
        return False
    letters = [char for char in title if char.isalpha()]
    upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters) if letters else 0
    if upper_ratio >= 0.55:
        return True
    # ponytail: roman lists are noisy in long RFP prose; dictionary hits preserve real mixed-case annexure titles.
    return bool(procurement_dictionary_entries_for_text(title))


def annexure_document_keys(title: str, annexure_no: str, source_text: str = "") -> list[str]:
    keys = [
        str(entry.get("canonical_key"))
        for entry in procurement_dictionary_entries_for_text(f"{title}\n{source_text}")
        if entry.get("canonical_key")
    ]
    keys.extend(inferred_annexure_document_keys(title, annexure_no, source_text))
    return sorted(dict.fromkeys(keys))


def inferred_annexure_document_keys(title: str, annexure_no: str, source_text: str = "") -> list[str]:
    normalized = normalize_for_match(f"{title} {annexure_no} {source_text}")
    keys = []
    if ("details of the bidder" in normalized or normalize_annexure_no(annexure_no) == "8") and "bidder" in normalized:
        keys.append("gst_registration")
    # ponytail: title heuristic for auction forms; move to dictionary metadata if more portal-specific forms appear.
    if "auction" in normalized and any(term in normalized for term in ["reverse", "letter of authority", "agreement between service provider"]):
        keys.append("reverse_auction_acceptance")
    return [key for key in keys if procurement_dictionary_entry_by_key(key)]


def infer_annexure_document_role(title: str, annexure_no: str, source_text: str = "") -> str:
    normalized = normalize_for_match(f"{title} {source_text}")
    if any(term in normalized for term in ["document upload", "documents to be submitted", "technical bid submission"]):
        return "bidder_submission_instruction"
    if any(term in normalized for term in ["declaration", "pact", "confirmation", "deviation", "undertaking", "certificate", "bid form", "cover letter", "format"]):
        return "bidder_submission_form"
    if any(term in normalized for term in ["evaluation criteria", "scope of work", "payment terms", "sla", "service level", "technical specification"]):
        return "tender_source_attachment"
    return "instruction"


def infer_annexure_submission(title: str, annexure_no: str, source_text: str = "") -> Any:
    normalized = normalize_for_match(f"{title} {source_text}")
    if page_has_bidder_submission_signal(normalized):
        return True
    if procurement_dictionary_entries_for_text(normalized) and any(term in normalized for term in ["form", "format", "declaration", "undertaking", "certificate", "bidder"]):
        return True
    return "unresolved"


def backfill_tender_requirements(extraction: TenderExtraction, annexures_by_no: dict[str, dict[str, Any]], tender_docs: list[ExtractedDocument]) -> None:
    for requirement in extraction.requirements:
        requirement_text = normalize_for_match(requirement.requirement)
        text = normalize_for_match(
            " ".join(
                [
                    requirement.requirement,
                    *[ref.quote for ref in requirement.source_refs if ref.quote],
                ]
            )
        )
        if tender_requirement_needs_bid_submission_form(text):
            add_requirement_document(requirement, "bid_submission_form")
        if tender_requirement_needs_acceptance_document(text):
            add_requirement_document(requirement, "tender_acceptance")
        if tender_requirement_needs_technical_commercial_compliance(text):
            add_requirement_document(requirement, "technical_commercial_compliance")
        if tender_requirement_needs_solvency_document(text):
            add_requirement_document(requirement, "solvency_bank_certificate")
        if tender_requirement_needs_local_content_document(requirement_text):
            add_requirement_document(requirement, "local_content_declaration")


def tender_requirement_needs_bid_submission_form(text: str) -> bool:
    return (
        ("online bid" in text or "e tender" in text or "e bid" in text)
        and any(term in text for term in ["submit", "submitted", "submission"])
    ) or ("prescribed format" in text and any(term in text for term in ["reject", "liable"]))


def tender_requirement_needs_acceptance_document(text: str) -> bool:
    return (
        ("terms and conditions" in text and any(term in text for term in ["offer", "bid", "submit"]))
        or ("prescribed format" in text and any(term in text for term in ["reject", "liable"]))
    )


def tender_requirement_needs_technical_commercial_compliance(text: str) -> bool:
    return any(term in text for term in ["techno commercial", "techno-commercial", "technical commercial", "technical-commercial"])


def tender_requirement_needs_solvency_document(text: str) -> bool:
    return "npa" in text or "non-performing asset" in text or ("solvency" in text and "bank" in text)


def tender_requirement_needs_local_content_document(text: str) -> bool:
    return (
        "ppp-mii" in text
        or "preference to make in india" in text
        or "public procurement preference to make in india" in text
    ) and "not applicable" not in text


def add_requirement_document(requirement: TenderRequirementExtraction, key: str) -> None:
    if any(document.canonical_key == key for document in requirement.required_documents):
        return
    requirement.required_documents.append(TenderEvidenceDocument(**normalize_extraction_document({"canonical_key": key})))


def backfill_annexure_required_documents(extraction: TenderExtraction, annexures_by_no: dict[str, dict[str, Any]]) -> None:
    required_by_key = {doc.canonical_key: doc for doc in extraction.required_documents}
    requirement_ids_by_key: dict[str, list[str]] = {}
    for requirement in extraction.requirements:
        for document in requirement.required_documents:
            if document.canonical_key:
                requirement_ids_by_key.setdefault(document.canonical_key, []).append(requirement.requirement_id)
    for annexure_no, annexure in annexures_by_no.items():
        for key in annexure.get("canonical_document_keys", []):
            source_annexures = [
                value
                for value in [annexure_no, str(annexure.get("title") or "").strip()]
                if value
            ]
            requirement_ids = requirement_ids_by_key.get(key, [])
            merge_required_document(
                required_by_key,
                key,
                requirement_ids=requirement_ids,
                source_annexures=source_annexures,
                extraction_status="listed_only" if not requirement_ids else "referenced_not_available",
                mandatory_status="unresolved" if not requirement_ids else "required",
            )
    extraction.required_documents = list(required_by_key.values())


def backfill_required_documents_from_tender_pages(extraction: TenderExtraction, tender_docs: list[ExtractedDocument]) -> None:
    required_by_key = {doc.canonical_key: doc for doc in extraction.required_documents}
    for document in tender_docs:
        for page in document.pages:
            matches = procurement_dictionary_entries_for_text(page.text)
            if not matches:
                continue
            mandatory_status = "required" if page_has_mandatory_submission_signal(page.text) else "unresolved"
            for entry in matches:
                key = str(entry.get("canonical_key") or "").strip()
                if not key:
                    continue
                if key in PORTAL_SUBMISSION_CONTROL_KEYS:
                    continue
                if not procurement_entry_has_submission_source_line(entry, page.text):
                    continue
                merge_required_document(
                    required_by_key,
                    key,
                    extraction_status="extracted",
                    mandatory_status=mandatory_status,
                )
    extraction.required_documents = list(required_by_key.values())


def procurement_entry_has_submission_source_line(entry: dict[str, Any], page_text: str) -> bool:
    page_normalized = normalize_for_match(page_text)
    terms = [
        str(entry.get("canonical_key") or "").replace("_", " "),
        str(entry.get("display_name") or ""),
        *[str(alias) for alias in entry.get("aliases", []) if str(alias).strip()],
    ]
    normalized_terms = [normalize_for_match(term) for term in terms if normalize_for_match(term)]
    key = str(entry.get("canonical_key") or "")
    for line in page_text.splitlines():
        clean = clean_text(line)
        if len(clean) < 8:
            continue
        normalized_line = normalize_for_match(clean)
        if not any(normalized_term_matches_text(term, normalized_line) for term in normalized_terms):
            continue
        if key == "similar_work_scope_proof" and not similar_work_scope_source_line(normalized_line, page_normalized):
            continue
        if (
            page_has_bidder_submission_signal(clean)
            or page_has_mandatory_submission_signal(clean)
            or tender_requirement_needs_acceptance_document(normalized_line)
            or submission_inventory_line(clean, page_normalized)
            or source_line_requires_document(key, normalized_line)
        ):
            return True
    return False


def similar_work_scope_source_line(normalized_line: str, page_normalized: str) -> bool:
    if any(term in normalized_line for term in ["similar work", "similar assignment", "client letter"]):
        return True
    return "scope" in normalized_line and any(
        term in page_normalized
        for term in ["bid qualification", "pre qualification", "eligibility criteria", "prior experience", "similar work"]
    )


def source_line_requires_document(key: str, normalized_line: str) -> bool:
    if key == "bid_validity_declaration":
        return any(term in normalized_line for term in ["offer validity", "bid validity", "tender validity"])
    if key == "technical_spec_compliance":
        return any(term in normalized_line for term in ["technical specification", "technical specifications"])
    if key == "business_registration":
        return any(
            term in normalized_line
            for term in ["registered company", "registered firm", "certificate of incorporation", "certificate of registration"]
        )
    return any(
        term in normalized_line
        for term in [
            "copy of",
            "documentary evidence",
            "documentary proof",
            "proof to be submitted",
            "supporting document",
            "supporting required",
            "certificate",
            "certified",
            "authorization",
            "authorisation",
            "letter from",
            "form",
            "format",
            "signed",
            "submitted",
            "to be provided",
            "shall be provided",
        ]
    )


def submission_inventory_line(line: str, page_normalized: str) -> bool:
    if not any(
        term in page_normalized
        for term in [
            "annexure",
            "checklist",
            "documents to be submitted",
            "document upload",
            "technical bid submission",
            "tender document consists",
            "following annexures",
            "enclosed",
        ]
    ):
        return False
    clean = line.strip()
    if re.match(r"^(?:[IVXLCDM]+\.|\d+\.|[a-z]\)|\([a-z]\))\s+", clean, flags=re.IGNORECASE):
        return True
    letters = [ch for ch in clean if ch.isalpha()]
    return bool(letters) and sum(1 for ch in letters if ch.isupper()) / len(letters) > 0.7


def normalized_term_matches_text(term: str, text: str) -> bool:
    if not term:
        return False
    return f" {term} " in f" {text} "


def merge_required_document(
    required_by_key: dict[str, TenderRequiredDocument],
    key: str,
    requirement_ids: list[str] | None = None,
    source_annexures: list[str] | None = None,
    extraction_status: str = "extracted",
    mandatory_status: str = "required",
) -> TenderRequiredDocument:
    normalized = normalize_required_extraction_document(
        {
            "canonical_key": key,
            "required_for_requirement_ids": requirement_ids or [],
            "source_annexures": source_annexures or [],
            "extraction_status": extraction_status,
            "mandatory_status": mandatory_status,
        }
    )
    existing = required_by_key.get(key)
    if not existing:
        existing = TenderRequiredDocument(**normalized)
        required_by_key[key] = existing
        return existing
    for requirement_id in normalized["required_for_requirement_ids"]:
        if requirement_id not in existing.required_for_requirement_ids:
            existing.required_for_requirement_ids.append(requirement_id)
    for annexure_no in normalized["source_annexures"]:
        if annexure_no not in existing.source_annexures:
            existing.source_annexures.append(annexure_no)
    if len(existing.checks) <= 1 and len(normalized["checks"]) > len(existing.checks):
        existing.checks = normalized["checks"]
    if existing.extraction_status == "extracted" and extraction_status != "extracted":
        existing.extraction_status = extraction_status
    if existing.mandatory_status in {"required", ""} and mandatory_status == "unresolved" and not existing.required_for_requirement_ids:
        existing.mandatory_status = "unresolved"
    return existing


def backfill_tender_packet_gaps(extraction: TenderExtraction, annexures_by_no: dict[str, dict[str, Any]], tender_docs: list[ExtractedDocument]) -> None:
    existing = {
        normalize_for_match(str(item.get("attachment") or ""))
        for item in extraction.tender_packet_gaps.missing_referenced_attachments
        if isinstance(item, dict)
    }
    for annexure in annexures_by_no.values():
        if annexure.get("presence_in_uploaded_pdf") == "body_present":
            continue
        if annexure.get("document_role") not in {"tender_source_attachment", "instruction", "bidder_submission_instruction"}:
            continue
        add_tender_gap(
            extraction,
            existing,
            standardized_missing_attachment_label(annexure),
            "Referenced or listed in the tender packet, but the full attachment body/details were not available in extracted source artifacts.",
            "Related criteria, form format, or document checks may be incomplete.",
        )
    required_document_keys = {
        doc.canonical_key
        for doc in extraction.required_documents
    } | {
        doc.canonical_key
        for requirement in extraction.requirements
        for doc in requirement.required_documents
    }
    if "financial_bid_boq" in required_document_keys and not has_present_price_schedule_annexure(extraction):
        add_tender_gap(
            extraction,
            existing,
            "Detailed BOQ / price schedule",
            "A separate price bid or BOQ is referenced by the tender flow, but the detailed price schedule body is not present in the uploaded source packet.",
            "Commercial bid fields and item-level price checks may be incomplete.",
        )
    if source_packet_looks_like_short_wrapper(extraction):
        add_tender_gap(
            extraction,
            existing,
            "Portal tender summary or schedule page",
            "The uploaded source behaves like a short NIT/IFB wrapper; portal schedule metadata may live outside the PDF.",
            "Bid dates, portal-specific controls, and schedule details may need portal data or additional attachments.",
        )

    if not extraction.tender_packet_gaps.missing_referenced_attachments:
        return

    fields = {
        str(item.get("field", "")).lower()
        for item in extraction.tender_packet_gaps.not_extractable_fields
        if isinstance(item, dict)
    }
    for field, status in [
        ("emd_or_bid_security", "not_present_in_uploaded_document"),
        ("tender_fee", "not_present_in_uploaded_document"),
        ("bqc_thresholds", "referenced_not_available"),
        ("turnover_requirement", "not_present_in_uploaded_document"),
        ("similar_work_requirement", "not_present_in_uploaded_document"),
        ("net_worth_requirement", "not_present_in_uploaded_document"),
        ("technical_scoring_or_pass_fail_method", "referenced_not_available"),
        ("price_evaluation_method", "not_present_in_uploaded_document"),
    ]:
        if field not in fields:
            extraction.tender_packet_gaps.not_extractable_fields.append({"field": field, "value": None, "status": status})


def standardized_missing_attachment_label(annexure: dict[str, Any]) -> str:
    annexure_no = normalize_annexure_no(annexure.get("annexure_no"))
    title = clean_annexure_title(str(annexure.get("title") or ""))
    normalized = normalize_for_match(title)
    if "bid qualification criteria" in normalized and "document upload" not in normalized:
        title = "Bid Qualification Criteria"
    elif "document upload" in normalized and "bid qualification criteria" in normalized:
        title = "Document Upload for Bid Qualification Criteria"
    elif "technical specification" in normalized and "drawings" in normalized:
        title = "Technical Specification / Drawings / SCC / Payment Terms"
    return f"Annexure {annexure_no} - {title}" if annexure_no else title


def has_present_price_schedule_annexure(extraction: TenderExtraction) -> bool:
    for annexure in extraction.annexures:
        title = normalize_for_match(str(annexure.get("title") or ""))
        if annexure.get("presence_in_uploaded_pdf") == "body_present" and any(
            term in title for term in ["commercial bid", "price schedule", "boq"]
        ):
            return True
    return False


def source_packet_looks_like_short_wrapper(extraction: TenderExtraction) -> bool:
    for source in extraction.source_documents:
        file_name = normalize_for_match(str(source.get("file_name") or ""))
        scope = normalize_for_match(str(source.get("content_scope") or ""))
        page_count = int(source.get("page_count") or 0)
        if page_count <= 12 and any(term in f"{file_name} {scope}" for term in ["nit", "ifb", "open tender"]):
            return True
    return False


def add_tender_gap(extraction: TenderExtraction, existing: set[str], attachment: str, why_needed: str, impact: str) -> bool:
    normalized = normalize_for_match(attachment)
    if normalized in existing:
        return False
    extraction.tender_packet_gaps.missing_referenced_attachments.append(
        {"attachment": attachment, "why_needed": why_needed, "impact_on_extraction": impact}
    )
    existing.add(normalized)
    return True


def backfill_tender_extraction(extraction: TenderExtraction, tender_docs: list[ExtractedDocument]) -> TenderExtraction:
    if not extraction.source_documents:
        extraction.source_documents = tender_source_documents(tender_docs)
    annexures_by_no = backfill_tender_annexures(extraction, tender_docs)
    backfill_tender_requirements(extraction, annexures_by_no, tender_docs)
    backfill_annexure_required_documents(extraction, annexures_by_no)
    backfill_required_documents_from_tender_pages(extraction, tender_docs)
    backfill_tender_packet_gaps(extraction, annexures_by_no, tender_docs)
    required_by_key = {doc.canonical_key: doc for doc in extraction.required_documents}
    for requirement in extraction.requirements:
        for document in requirement.required_documents:
            if not document.canonical_key:
                continue
            if document.canonical_key not in required_by_key:
                required_by_key[document.canonical_key] = TenderRequiredDocument(
                    **model_dump(document),
                    required_for_requirement_ids=[requirement.requirement_id],
                    extraction_status=requirement.extraction_status,
                    mandatory_status="required" if requirement.mandatory else "optional",
                )
            elif requirement.requirement_id not in required_by_key[document.canonical_key].required_for_requirement_ids:
                required_by_key[document.canonical_key].required_for_requirement_ids.append(requirement.requirement_id)
    extraction.required_documents = list(required_by_key.values())
    prune_unowned_required_documents(extraction)
    extraction.audit.setdefault("derived_by", "tender_extraction_agent")
    return extraction


def prune_unowned_required_documents(extraction: TenderExtraction) -> None:
    requirement_keys = {
        document.canonical_key
        for requirement in extraction.requirements
        for document in requirement.required_documents
        if document.canonical_key
    }
    has_bqc_specific = bool(requirement_keys & BQC_SPECIFIC_DOCUMENT_KEYS)
    pruned = []
    for document in extraction.required_documents:
        key = document.canonical_key
        if document.required_for_requirement_ids:
            pruned.append(document)
            continue
        if key == "bqc_qualification_documents" and has_bqc_specific:
            continue
        if document_key_needs_requirement_owner(key) and not document_has_supporting_source_annexure(document):
            continue
        pruned.append(document)
    extraction.required_documents = pruned


def document_has_supporting_source_annexure(document: TenderRequiredDocument) -> bool:
    if not document.source_annexures:
        return False
    entry = procurement_dictionary_entry_by_key(document.canonical_key)
    terms = [
        str(entry.get("display_name") or ""),
        str(entry.get("canonical_key") or "").replace("_", " "),
        *[str(alias) for alias in entry.get("aliases", []) if str(alias).strip()],
    ]
    haystack = normalize_for_match(" ".join(document.source_annexures))
    haystack_tokens = set(haystack.split())
    for term in terms:
        normalized_term = normalize_for_match(term)
        if not normalized_term:
            continue
        if len(normalized_term) <= 4:
            if normalized_term in haystack_tokens:
                return True
            continue
        if re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", haystack):
            return True
    return False
