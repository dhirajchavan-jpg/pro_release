"""Logical-document construction from classified source pages."""

from __future__ import annotations

from procurement.domain.schemas import (
    DocumentClassification,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
)
from procurement.domain.text import unique_items


# Human-readable display names for classification buckets; the title of a
# classified document should say what the document IS, never raw page text.
_LOGICAL_DOCUMENT_TYPE_TITLES = {
    "gem_bid": "GeM Bid Summary",
    "rfp_or_bid_document": "Bid / RFP Document",
    "atc_stc_sla_gtc": "ATC / STC / GTC Terms",
    "corrigendum": "Corrigendum",
    "prebid_clarification": "Pre-Bid Clarification",
    "eligibility_pqc": "PQR / Pre-Qualification Response",
    "mse_udyam_or_startup_exemption": "Udyam / MSE Registration Certificate",
    "mii_local_content": "Local Content (Make in India) Declaration",
    "land_border_declaration": "Land Border (Rule 144-xi) Declaration",
    "oem_authorization": "OEM / Channel Partner Authorization",
    "poa_or_board_authorization": "Power of Attorney / Board Authorization",
    "experience_po_completion": "Experience / PO Completion Evidence",
    "past_work_evidence": "Past Work / Delivery Proof",
    "quality_certificate": "Quality / Test Certificate",
    "calibration_certificate": "Calibration Certificate",
    "technical_specs_or_sow": "Technical Specification",
    "datasheet_catalogue": "Datasheet / Product Catalogue",
    "technical_proposal": "Technical Proposal",
    "techno_commercial_compliance": "Techno-Commercial Compliance",
    "integrity_pact": "Integrity Pact",
    "integrity_blacklisting_declaration": "Integrity / Blacklisting Declaration",
    "non_blacklisting_declaration": "Non-Blacklisting Declaration",
    "nil_deviation_declaration": "Nil-Deviation Declaration",
    "tender_acceptance": "Tender Acceptance",
    "bid_security_declaration": "Bid Security Declaration",
    "emd_or_bid_security": "EMD / Bid Security Proof",
    "bank_guarantee": "Bank Guarantee",
    "turnover_networth_ca_certificate": "Turnover / Net Worth CA Certificate",
    "audited_financials_or_itr": "Audited Financials / ITR",
    "bank_solvency": "Bank Solvency Certificate",
    "gst_pan_cin_statutory": "Statutory Registrations (PAN / GST / CIN)",
    "gem_registration": "GeM Registration",
    "registration_document": "Registration Document",
    "statutory_license": "Statutory License",
    "environmental_statutory": "Environmental / Statutory Consent",
    "labour_compliance": "Labour Compliance",
    "consortium_jv": "Consortium / JV Agreement",
    "bid_covering_letter": "Covering Letter",
    "bid_submission": "Bid Submission",
    "forms_annexures": "Forms / Annexures",
    "boq_or_price_schedule": "BOQ / Price Schedule",
    "commercial_or_price": "Commercial / Price Bid",
    "financial_bid": "Financial Bid",
    "contract_terms": "Contract Terms",
    "tender_notice": "Tender Notice",
    "other": "Other Document",
    "unknown": "Unclassified Document",
}


def _logical_document_type_title(document_type: str) -> str:
    return _LOGICAL_DOCUMENT_TYPE_TITLES.get(
        document_type, document_type.replace("_", " ").title()
    )


def make_logical_document(
    counter: int,
    document: ExtractedDocument,
    pages_or_refs: list[PageUnit] | list[str],
    classification: DocumentClassification,
) -> LogicalDocument:
    page_refs = [
        item.page_id if isinstance(item, PageUnit) else item
        for item in pages_or_refs
    ]
    title = classification.title or document.record.file_name
    if pages_or_refs and isinstance(pages_or_refs[0], PageUnit):
        first = pages_or_refs[0]
        last = pages_or_refs[-1]
        type_title = _logical_document_type_title(classification.document_type)
        if first.page_number != last.page_number:
            title = f"{document.record.file_name} pages {first.page_number}-{last.page_number}: {type_title}"
        elif title == document.record.file_name:
            # Single-page document with no classifier title: show what the
            # document IS, never a raw page line or a bare filename.
            title = f"{type_title} (page {first.page_number})"
    return LogicalDocument(
        logical_document_id=f"LD-{counter}",
        upload_side=document.record.upload_side,
        title=title,
        document_type=classification.document_type,
        content_role=classification.content_role,
        input_role=document.record.input_role,
        source_document_ids=[document.record.document_id],
        source_files=[document.record.file_name],
        page_refs=page_refs,
        key_facts=unique_items(
            [
                fact
                for item in pages_or_refs
                if isinstance(item, PageUnit) and item.classification
                for fact in item.classification.key_facts
            ]
            + list(classification.key_facts)
        ),
        confidence=classification.confidence,
        status="reconstructed" if len(page_refs) > 1 else "found",
    )


__all__ = [
    "_LOGICAL_DOCUMENT_TYPE_TITLES",
    "_logical_document_type_title",
    "make_logical_document",
]
