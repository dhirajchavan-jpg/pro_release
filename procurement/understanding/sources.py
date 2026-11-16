"""Authoritative tender-source and amendment selection."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from procurement.domain.catalog import BASE_RFP_DOCUMENT_TYPES, POST_BASE_RFP_DOCUMENT_TYPES
from procurement.domain.schemas import ExtractedDocument, LogicalDocument, PageUnit


_AMENDMENT_NUMBER_RE = re.compile(
    r"\b(?:corrigendum|addendum|amendment)\s*(?:no\.?|number|#)?\s*[-:./]?\s*(\d+)\b",
    re.IGNORECASE,
)
_AMENDMENT_FILENAME_NUMBER_RE = re.compile(
    r"(?:corrigendum|addendum|amendment)[ _.-]*(\d+)\b",
    re.IGNORECASE,
)
_AMENDMENT_DATE_RE = re.compile(
    r"\b(?:corrigendum|addendum|amendment)\b[^\n]{0,100}?"
    r"\b(?:dated|issued\s+on)\b[^\n]{0,18}?"
    r"(\d{1,2}(?:st|nd|rd|th)?[./ -]\d{1,2}[./ -]\d{2,4}|"
    r"\d{1,2}(?:st|nd|rd|th)?[ -](?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
    r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)[, -]+\d{2,4})",
    re.IGNORECASE,
)


def _parse_amendment_date(text: str) -> str | None:
    for match in _AMENDMENT_DATE_RE.finditer(text[:12000]):
        value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", match.group(1), flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value.replace(",", " ")).strip()
        for fmt in (
            "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%y", "%d-%m-%y", "%d/%m/%y",
            "%d %B %Y", "%d %b %Y", "%d %B %y", "%d %b %y",
        ):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _amendment_metadata(
    logical: LogicalDocument,
    page_by_id: dict[str, PageUnit],
) -> dict[str, Any]:
    body = "\n".join(
        (page_by_id[page_id].layout_text or page_by_id[page_id].text)
        for page_id in logical.page_refs[:6]
        if page_id in page_by_id
    )
    body_numbers = [int(match.group(1)) for match in _AMENDMENT_NUMBER_RE.finditer(body)]
    body_match = _AMENDMENT_NUMBER_RE.search(body)
    filename = " ".join(logical.source_files or [logical.title])
    filename_match = _AMENDMENT_FILENAME_NUMBER_RE.search(filename)
    body_number = int(body_match.group(1)) if body_match else None
    filename_number = int(filename_match.group(1)) if filename_match else None
    number_conflict = (
        body_number is not None
        and filename_number is not None
        and body_number != filename_number
    ) or len(set(body_numbers)) > 1
    issue_date = _parse_amendment_date(body)
    return {
        "revision_number": filename_number if number_conflict else (body_number or filename_number),
        "revision_body_number": body_number,
        "revision_filename_number": filename_number,
        "revision_number_source": "body" if body_number is not None else ("filename" if filename_number is not None else None),
        "revision_date": issue_date,
        "revision_number_conflict": number_conflict,
        "revision_metadata_complete": bool(issue_date or (body_number and not number_conflict) or filename_number),
    }


def _is_policy_reference_source(
    logical: LogicalDocument,
    page_by_id: dict[str, PageUnit],
) -> bool:
    if logical.content_role == "reference":
        return True
    parts = [logical.title, *logical.source_files]
    for page_id in logical.page_refs:
        page = page_by_id.get(page_id)
        if not page:
            continue
        parts.append((page.layout_text or page.text)[:1200])
        if sum(len(part) for part in parts) >= 8000:
            break
    text = " ".join(parts)[:8000].lower()
    return bool(
        ("relaxation" in text or "relaxsation" in text) and "norm" in text
        or "office memorandum" in text
        or "policy circular" in text
        or "public procurement policy" in text
    )


def _assign_amendment_order(selections: list[dict[str, Any]]) -> None:
    updates = [item for item in selections if item.get("role") == "post_base_update"]
    if not updates:
        return
    for item in updates:
        item["authority_tier"] = (
            "formal_amendment"
            if item.get("document_type") == "corrigendum"
            else "official_clarification"
        )
        item["revision_order_warning"] = ""
    ambiguous = [
        item for item in updates
        if item.get("document_type") == "corrigendum"
        and (item.get("revision_number_conflict") or not item.get("revision_number"))
    ]
    numbered = [
        int(item["revision_number"])
        for item in updates
        if item.get("document_type") == "corrigendum" and item.get("revision_number")
    ]
    duplicate_numbers = {number for number in numbered if numbered.count(number) > 1}
    ambiguous.extend(
        item
        for item in updates
        if item.get("document_type") == "corrigendum"
        and item.get("revision_number") in duplicate_numbers
        and item not in ambiguous
    )
    if ambiguous:
        for item in ambiguous:
            item["revision_order_warning"] = (
                "Corrigendum identity or numbering conflicts with another source; "
                "formal changes were withheld for human review."
            )
        return
    ordered = sorted(
        updates,
        key=lambda item: (
            1 if item.get("document_type") == "corrigendum" else 0,
            int(item.get("revision_number") or 999999),
            item.get("revision_date") or "9999-12-31",
            int(item.get("document_order") or 0),
        ),
    )
    for sequence, item in enumerate(ordered, start=1):
        item["revision_sequence"] = sequence
        item["chronology_basis"] = "document_date_and_number"


def identify_base_rfp_set(
    logical_documents: list[LogicalDocument],
    tender_pages: list[PageUnit] | None = None,
) -> list[dict[str, Any]]:
    tender_documents = [item for item in logical_documents if item.upload_side == "tender"]
    packet_documents = [item for item in tender_documents if item.input_role == "tender_packet"]
    has_explicit_base = any(
        item.document_type in {"rfp_or_bid_document", "gem_bid"}
        for item in packet_documents
    )
    page_by_id = {page.page_id: page for page in (tender_pages or [])}
    selections: list[dict[str, Any]] = []
    for document_order, item in enumerate(tender_documents, start=1):
        document_type = item.document_type
        policy_reference = _is_policy_reference_source(item, page_by_id)
        if (
            policy_reference
            and document_type in {"rfp_or_bid_document", "gem_bid"}
            and item.content_role != "reference"
        ):
            # GeM/bid boilerplate quotes policy phrases inside the bid document itself.
            policy_reference = False
        metadata = (
            _amendment_metadata(item, page_by_id)
            if document_type == "corrigendum"
            else {}
        )
        if item.input_role != "tender_packet" or policy_reference:
            role = "evaluation_guideline"
            included = False
            reason = "Supplied policy/reference guidance is not part of the tender's authoritative source set."
        elif document_type in POST_BASE_RFP_DOCUMENT_TYPES:
            role = "post_base_update"
            included = False
            reason = "Reserved for the amendment/clarification layer after base-RFP understanding."
        elif document_type in {"rfp_or_bid_document", "gem_bid"}:
            role = "base_rfp"
            included = True
            reason = "Explicit base RFP/bid document."
        elif document_type in BASE_RFP_DOCUMENT_TYPES:
            role = "base_attachment" if document_type != "tender_notice" else "base_notice"
            included = True
            reason = "Tender-side source that forms part of the authoritative base packet."
        elif document_type == "unknown":
            role = "base_unresolved"
            included = True
            reason = "Unknown tender-side source retained conservatively so no base content is skipped."
        else:
            role = "supporting_source"
            included = not has_explicit_base
            reason = (
                "Included only when no explicit base RFP was identified."
                if included
                else "Not part of the authoritative base set."
            )
        selections.append(
            {
                "logical_document_id": item.logical_document_id,
                "document_type": document_type,
                "role": role,
                "included": included,
                "reason": reason,
                "confidence": item.confidence,
                "source_document_ids": item.source_document_ids,
                "source_files": item.source_files,
                "page_refs": item.page_refs,
                "document_order": document_order,
                **metadata,
            }
        )
    _assign_amendment_order(selections)
    if packet_documents and not any(item["included"] for item in selections):
        for item in selections:
            if item["role"] not in {"post_base_update", "evaluation_guideline"}:
                item["included"] = True
                item["role"] = "base_unresolved"
                item["reason"] = (
                    "Fallback inclusion because no authoritative base source was otherwise available."
                )
    return selections


def selected_base_rfp_pages(
    selections: list[dict[str, Any]],
    tender_docs: list[ExtractedDocument],
) -> list[PageUnit]:
    included_page_ids = {
        page_id
        for selection in selections
        if selection.get("included")
        for page_id in selection.get("page_refs") or []
    }
    pages = [
        page
        for document in tender_docs
        for page in document.pages
        if page.page_id in included_page_ids
    ]
    if pages:
        return pages
    return [
        page
        for document in tender_docs
        if document.record.input_role == "tender_packet"
        for page in document.pages
    ]


def selected_base_rfp_documents(
    selections: list[dict[str, Any]],
    tender_docs: list[ExtractedDocument],
) -> list[ExtractedDocument]:
    document_ids = {
        page.document_id for page in selected_base_rfp_pages(selections, tender_docs)
    }
    return [
        document
        for document in tender_docs
        if document.record.document_id in document_ids
    ]


def selected_post_base_rfp_pages(
    selections: list[dict[str, Any]],
    tender_docs: list[ExtractedDocument],
) -> list[PageUnit]:
    page_ids = {
        page_id
        for selection in selections
        if selection.get("role") == "post_base_update"
        for page_id in selection.get("page_refs") or []
    }
    return [
        page
        for document in tender_docs
        for page in document.pages
        if page.page_id in page_ids
    ]
