"""Bounded extraction of bidder routing metadata."""

import json
import os
from typing import Any

from procurement.domain.schemas import ExtractedDocument, LogicalDocument, TenderProfile, VendorProfile
from procurement.domain.serialization import model_dump, validate_model
from procurement.domain.text import clean_text, unique_items
from procurement.evaluation.normalization import normalize_vendor_profile_data
from procurement.platform.llm import llm_json
from procurement.platform.prompts import prompt_text


VENDOR_PROFILE_BATCH_DOCUMENTS = max(1, int(os.getenv("VENDOR_PROFILE_BATCH_DOCUMENTS", "12")))
VENDOR_PROFILE_BATCH_CHARS = max(5000, int(os.getenv("VENDOR_PROFILE_BATCH_CHARS", "40000")))
VENDOR_PROFILE_DOCUMENT_CHARS = max(1000, int(os.getenv("VENDOR_PROFILE_DOCUMENT_CHARS", "12000")))


def extract_vendor_profile(
    vendor_docs: list[ExtractedDocument],
    tender: TenderProfile,
    logical_documents: list[LogicalDocument] | None = None,
) -> VendorProfile:
    """Extract routing metadata in bounded batches; criteria use the full evidence index."""
    pages_by_id = {
        page.page_id: page
        for document in vendor_docs
        for page in document.pages
    }
    units: list[dict[str, Any]] = []
    for logical in logical_documents or []:
        if logical.upload_side != "vendor":
            continue
        source_text = clean_text(
            "\n".join(
                page.text
                for page_id in logical.page_refs
                if (page := pages_by_id.get(page_id)) is not None and page.text
            )
        )[:VENDOR_PROFILE_DOCUMENT_CHARS]
        units.append(
            {
                "logical_document_id": logical.logical_document_id,
                "title": logical.title,
                "document_type": logical.document_type,
                "labels": list(logical.labels),
                "capabilities": list(logical.capabilities),
                "source_files": list(logical.source_files),
                "page_refs": list(logical.page_refs),
                "content_summary": logical.content_summary,
                "key_facts": list(logical.key_facts),
                "text_excerpt": source_text,
            }
        )
    if not units:
        for document in vendor_docs:
            document_text = clean_text(document.text)
            if len(document_text) > VENDOR_PROFILE_DOCUMENT_CHARS:
                slice_chars = max(1, VENDOR_PROFILE_DOCUMENT_CHARS // 3)
                middle = max(0, (len(document_text) - slice_chars) // 2)
                document_text = "\n".join(
                    (
                        document_text[:slice_chars],
                        document_text[middle:middle + slice_chars],
                        document_text[-slice_chars:],
                    )
                )[:VENDOR_PROFILE_DOCUMENT_CHARS]
            units.append(
                {
                    "document_id": document.record.document_id,
                    "file_name": document.record.file_name,
                    "classification": model_dump(document.record.classification)
                    if document.record.classification
                    else None,
                    "text_excerpt": document_text,
                }
            )

    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    for unit in units:
        unit_chars = len(json.dumps(unit, ensure_ascii=True))
        if batch and (
            len(batch) >= VENDOR_PROFILE_BATCH_DOCUMENTS
            or batch_chars + unit_chars > VENDOR_PROFILE_BATCH_CHARS
        ):
            batches.append(batch)
            batch = []
            batch_chars = 0
        batch.append(unit)
        batch_chars += unit_chars
    if batch:
        batches.append(batch)

    profiles: list[VendorProfile] = []
    tender_context = {
        "tender_title": tender.tender_title,
        "tender_number": tender.tender_number,
        "buyer": tender.buyer,
        "procurement_type": tender.procurement_type,
    }
    for batch_index, documents in enumerate(batches, start=1):
        data = llm_json(
            prompt_text("vendor_profile", "You are a bid-submission analyst. Extract bidder facts from vendor documents only. Return only JSON."),
            {
                "task": "extract_vendor_profile",
                "batch": {"number": batch_index, "count": len(batches)},
                "tender_context": tender_context,
                "rules": [
                    "Extract only facts present in this bidder batch; never infer a missing fact from the tender.",
                    "Keep bidder, director, customer, issuer, and related-party identifiers distinct in the identifier key names.",
                    "This is routing metadata, not a pass/fail decision.",
                ],
                "documents": documents,
                "json_shape": {
                    "vendor_name": "string",
                    "identifiers": {"entity_and_identifier_type": "value if found"},
                    "contact": {"address_email_phone": "value if found"},
                    "documents_submitted": ["submitted evidence documents"],
                    "claimed_experience": ["work orders/projects/POs"],
                    "financials": {"turnover_networth_or_statements": "value if found"},
                    "commercial_offer": {"price_or_payment_terms": "value if found"},
                    "declarations": ["integrity, blacklisting, compliance, make in india, mse declarations"],
                    "extraction_warnings": ["ambiguities or unreadable evidence"],
                },
            },
            thinking_budget=0,
        )
        normalize_vendor_profile_data(data)
        profiles.append(validate_model(VendorProfile, data))

    if not profiles:
        return VendorProfile(extraction_warnings=["No bidder documents were available for profile extraction."])

    names = [profile.vendor_name for profile in profiles if profile.vendor_name.casefold() != "unknown"]
    vendor_name = max(names, key=lambda value: (sum(item.casefold() == value.casefold() for item in names), -names.index(value))) if names else "Unknown"

    def merge_mappings(attribute: str) -> dict[str, str]:
        merged: dict[str, str] = {}
        for profile in profiles:
            for key, value in getattr(profile, attribute).items():
                existing = merged.get(key)
                values = unique_items([*(existing.split(" | ") if existing else []), value])
                merged[key] = " | ".join(values)
        return merged

    def merge_lists(attribute: str) -> list[str]:
        return unique_items([value for profile in profiles for value in getattr(profile, attribute)])

    return VendorProfile(
        vendor_name=vendor_name,
        identifiers=merge_mappings("identifiers"),
        contact=merge_mappings("contact"),
        documents_submitted=merge_lists("documents_submitted"),
        claimed_experience=merge_lists("claimed_experience"),
        financials=merge_mappings("financials"),
        commercial_offer=merge_mappings("commercial_offer"),
        declarations=merge_lists("declarations"),
        extraction_warnings=merge_lists("extraction_warnings"),
    )

