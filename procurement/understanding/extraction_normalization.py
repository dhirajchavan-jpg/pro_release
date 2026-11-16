"""Normalization of extracted tender payloads and document references."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.document_dictionary import (
    load_procurement_document_dictionary,
    procurement_dictionary_entries_for_text,
)
from procurement.domain.schemas import ExtractedDocument, TenderExtraction
from procurement.domain.text import clean_text, normalize_for_match

def procurement_dictionary_entry_by_key(key: str) -> dict[str, Any]:
    for entry in load_procurement_document_dictionary().get("entries", []):
        if isinstance(entry, dict) and entry.get("canonical_key") == key:
            return entry
    return {}

def compact_tender_partials(partials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [compact_tender_partial(partial) for partial in partials]


def compact_tender_partial(partial: dict[str, Any]) -> dict[str, Any]:
    return {
        "extraction_status": partial.get("extraction_status"),
        "tender": partial.get("tender", {}),
        "annexures": partial.get("annexures", []),
        "requirements": partial.get("requirements", []),
        "required_documents": partial.get("required_documents", []),
        "tender_packet_gaps": partial.get("tender_packet_gaps", {}),
    }


def supplied_tender_extraction(tender_docs: list[ExtractedDocument]) -> TenderExtraction | None:
    # Runtime must extract from the uploaded tender, not accept an answer-key JSON.
    return None


def normalize_tender_extraction_data(data: dict[str, Any], tender_docs: list[ExtractedDocument]) -> None:
    data["schema_version"] = "tender_extraction_v1"
    data.setdefault("dataset_case_id", None)
    data.setdefault("source_stage", "runtime")
    data.setdefault("source_documents", tender_source_documents(tender_docs))
    data.setdefault("tender", {})
    data.setdefault("annexures", [])
    data.setdefault("requirements", data.pop("criteria", []))
    data.setdefault("required_documents", [])
    data.setdefault("tender_packet_gaps", {})
    data.setdefault("audit", {})
    normalize_tender_identity_fields(data["tender"])
    if not data.get("extraction_status"):
        gaps = data.get("tender_packet_gaps", {})
        data["extraction_status"] = "partial_referenced_attachments_missing" if gaps.get("missing_referenced_attachments") else "complete"

    for index, requirement in enumerate(data.get("requirements") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        requirement.setdefault("requirement_id", requirement.get("criterion_id") or f"T1-C{index}")
        requirement.setdefault("category", "other")
        requirement.setdefault("stage", "other")
        requirement.setdefault("mandatory", True)
        requirement.setdefault("failure_effect", "reject_unresponsive")
        requirement.setdefault("extraction_status", "extracted")
        requirement.setdefault("missing_detail", "")
        if requirement.get("missing_detail") is None:
            requirement["missing_detail"] = ""
        requirement.setdefault("source_refs", [])
        if requirement.get("source_refs") is None:
            requirement["source_refs"] = []
        requirement["required_documents"] = [normalize_extraction_document(doc) for doc in requirement.get("required_documents") or []]
    data["required_documents"] = [normalize_required_extraction_document(doc) for doc in data.get("required_documents") or []]


def normalize_tender_identity_fields(tender: dict[str, Any]) -> None:
    number = normalize_tender_number_from_identifiers(
        str(tender.get("tender_number") or ""),
        tender.get("tender_identifiers") or [],
    )
    if number:
        tender["tender_number"] = number


def normalize_tender_number_from_identifiers(number: str, identifiers: list[Any]) -> str:
    number = clean_text(number)
    gem_numbers = {
        re.sub(r"\s+", "", match.group(0)).upper()
        for identifier in identifiers
        if isinstance(identifier, dict)
        for match in re.finditer(
            r"(?<![A-Za-z0-9])GEM\s*/\s*\d{4}\s*/\s*B\s*/\s*\d+(?![A-Za-z0-9])",
            str(identifier.get("value") or ""),
            re.IGNORECASE,
        )
    }
    if len(gem_numbers) == 1:
        return gem_numbers.pop()
    if re.search(r"[A-Za-z]", number) or not identifiers:
        return number
    for identifier in identifiers:
        if not isinstance(identifier, dict):
            continue
        id_type = normalize_for_match(str(identifier.get("type") or ""))
        value = clean_text(str(identifier.get("value") or ""))
        if normalize_for_match(value) != normalize_for_match(number):
            continue
        if id_type == "rfp_no":
            return f"RFP-{number}"
        if id_type == "nit_no":
            return f"NIT-{number}"
    return number


def tender_source_documents(tender_docs: list[ExtractedDocument]) -> list[dict[str, Any]]:
    sources = []
    for document in tender_docs:
        classification = document.record.classification
        sources.append(
            {
                "document_id": document.record.document_id,
                "file_name": document.record.file_name,
                "document_type": classification.document_type if classification else "unknown",
                "page_count": document.record.page_count,
                "extraction_status": document.record.extraction_status,
                "content_scope": clean_text(document.text[:240]),
            }
        )
    return sources


def normalize_extraction_document(document: Any) -> dict[str, Any]:
    if isinstance(document, str):
        matches = procurement_dictionary_entries_for_text(document)
        item = dict(matches[0]) if matches else {"display_name": document}
    elif isinstance(document, dict):
        item = dict(document)
        source_name = str(item.get("display_name") or item.get("document_name") or "")
        if any(term in source_name.lower() for term in ["oem authorization", "manufacturer authorization", "manufacturer authorisation"]):
            item = {**procurement_dictionary_entry_by_key("oem_authorization"), **item, "canonical_key": "oem_authorization"}
        if item.get("canonical_key"):
            item = {**procurement_dictionary_entry_by_key(str(item["canonical_key"])), **item}
        if not item.get("evidence_type") and item.get("display_name"):
            matches = procurement_dictionary_entries_for_text(str(item["display_name"]))
            if matches:
                item = {**item, **matches[0]}
    else:
        item = {}
    key = str(item.get("canonical_key") or normalize_for_match(str(item.get("display_name", ""))).replace(" ", "_")[:80])
    if key:
        item = {**procurement_dictionary_entry_by_key(key), **item}
    return {
        "canonical_key": key,
        "display_name": str(item.get("display_name") or key.replace("_", " ")),
        "document_type": str(item.get("evidence_type") or item.get("document_type") or "unknown"),
        "category": str(item.get("category") or "other"),
        "stage": str(item.get("stage") or "bid_stage"),
        "checks": [str(check) for check in item.get("checks") or ["presence"]],
    }


def normalize_required_extraction_document(document: Any) -> dict[str, Any]:
    item = normalize_extraction_document(document)
    if isinstance(document, dict):
        item["required_for_requirement_ids"] = [str(value) for value in document.get("required_for_requirement_ids") or []]
        item["source_annexures"] = [str(value) for value in document.get("source_annexures") or []]
        item["extraction_status"] = str(document.get("extraction_status") or "extracted")
        item["mandatory_status"] = str(document.get("mandatory_status") or "required")
    else:
        item["required_for_requirement_ids"] = []
        item["source_annexures"] = []
        item["extraction_status"] = "extracted"
        item["mandatory_status"] = "required"
    if item["mandatory_status"] == "needs_expert_review":
        item["mandatory_status"] = "unresolved"
    return item
