"""Tender and bidder document classification and logical grouping."""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from procurement.domain.catalog import DOC_TYPES, POST_BASE_RFP_DOCUMENT_TYPES, VENDOR_CLASSIFICATION_RULES
from procurement.domain.serialization import validate_model
from procurement.domain.schemas import (
    DocumentClassification,
    DocumentRecord,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
)
from procurement.domain.text import clean_text, normalize_for_match, unique_items
from procurement.platform import llm as llm_runtime
from procurement.platform.job_context import JOB_CONTEXT
from procurement.platform.prompts import prompt_text
from procurement.understanding.structure import make_logical_document


logger = logging.getLogger("procurement_evaluator")
LLM_CLASSIFY_CHARS = int(os.getenv("LLM_CLASSIFY_CHARS", "3000"))
MAX_LLM_PAGE_CLASSIFICATIONS = int(os.getenv("MAX_LLM_PAGE_CLASSIFICATIONS", "30"))
BID_CLASSIFICATION_CONCURRENCY = max(1, int(os.getenv("BID_CLASSIFICATION_CONCURRENCY", "4")))
BID_SUMMARY_BATCH_DOCUMENTS = max(1, int(os.getenv("BID_SUMMARY_BATCH_DOCUMENTS", "10")))
BID_SUMMARY_BATCH_CHARS = max(5000, int(os.getenv("BID_SUMMARY_BATCH_CHARS", "50000")))
BID_SUMMARY_DOCUMENT_CHARS = max(1000, int(os.getenv("BID_SUMMARY_DOCUMENT_CHARS", "30000")))


def heuristic_classification(document: ExtractedDocument) -> DocumentClassification | None:
    name = document.record.file_name.lower()
    text = document.text[:2000].lower()
    role = "tender_source" if document.record.upload_side == "tender" else "vendor_submission"
    if document.record.upload_side == "vendor" and tender_source_trap(f"{name}\n{text}"):
        return DocumentClassification(
            content_role="reference",
            document_type="rfp_or_bid_document",
            title=document.record.file_name,
            confidence=0.85,
            reason="Vendor upload repeats tender or contract source material but does not provide filled bidder evidence.",
            key_facts=[],
        )
    if document.record.upload_side == "tender" and any(
        marker in f"{name}\n{text}"
        for marker in ["tender document", "notice inviting tender", "request for proposal", "bid document", "e-tender"]
    ):
        return DocumentClassification(
            content_role=role,
            document_type="rfp_or_bid_document",
            title=document.record.file_name,
            confidence=0.8,
            reason="Matched tender-source document markers locally.",
            key_facts=[],
        )
    vendor_evidence_type = vendor_evidence_document_type(document.record, document.text)
    if vendor_evidence_type:
        return DocumentClassification(
            content_role="vendor_submission",
            document_type=vendor_evidence_type,
            title=document.record.file_name,
            confidence=0.85,
            reason="Matched vendor-submitted evidence markers locally.",
            key_facts=[],
        )
    checks = [
        ("gem_bid", ["gem-bidding", "bid details", "bid number"]),
        ("atc_stc_sla_gtc", ["buyer added", "additional terms", "general purchase condition", "gpc"]),
        ("mse_udyam_or_startup_exemption", ["udyam", "msme registration", "mse certificate", "enterprise type micro"]),
        ("land_border_declaration", ["country sharing land border", "land border with india", "rule 144 xi", "competent authority registration"]),
        ("integrity_pact", ["integrity pact", "independent external monitor"]),
        ("non_blacklisting_declaration", ["holiday", "banning", "blacklisted", "debar"]),
        ("nil_deviation_declaration", ["nil deviation", "no deviation"]),
        ("techno_commercial_compliance", ["techno-commercial bid", "techno commercial bid", "techno-commercial compliance", "techno commercial compliance", "unpriced commercial compliance"]),
        ("mii_local_content", ["local content", "public procurement", "make in india", "pplc"]),
        ("technical_proposal", ["technical specification", "technicalspec", "technical spec"]),
        ("datasheet_catalogue", ["technical data sheet", "property specification", "nominal thickness", "product catalogue"]),
        ("quality_certificate", ["iso ", "iso-", "quality management certificate", "quality certificate", "test certificate"]),
        ("corrigendum", ["corrigendum"]),
        ("boq_or_price_schedule", ["boq", "price schedule", "commercial details", "quoted"]),
    ]
    haystack = f"{name}\n{text}"
    for doc_type, needles in checks:
        if any(needle in haystack for needle in needles):
            return DocumentClassification(
                content_role=role,
                document_type=doc_type,
                title=document.record.file_name,
                confidence=0.75,
                reason="Matched filename/text procurement keywords locally.",
                key_facts=[],
            )
    return None


def normalize_classification(
    record: DocumentRecord,
    text: str,
    classification: DocumentClassification,
) -> DocumentClassification:
    vendor_evidence_type = vendor_evidence_document_type(record, text)
    if vendor_evidence_type:
        classification.content_role = "vendor_submission"
        if classification.document_type == "other":
            classification.document_type = vendor_evidence_type
        classification.reason = f"{classification.reason} Local vendor evidence override applied.".strip()
    return classification


def default_content_role(side: Literal["tender", "vendor"]) -> str:
    return "tender_source" if side == "tender" else "unknown"


def coerce_classification_data(data: Any, fallback_role: str = "unknown") -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    nested = data.get("classification")
    if isinstance(nested, dict) and not data.get("document_type"):
        data = {**nested, **data}
    if data.get("content_role") not in {"tender_source", "vendor_submission", "reference", "unknown"}:
        data["content_role"] = fallback_role
    if data.get("document_type") not in DOC_TYPES:
        data["document_type"] = "unknown"
    if "key_facts" not in data and isinstance(data.get("key_findings"), list):
        data["key_facts"] = data["key_findings"]
    data.setdefault("title", None)
    data.setdefault("confidence", 0.0)
    data.setdefault("reason", "Classifier returned incomplete JSON.")
    data.setdefault("key_facts", [])
    return data


def vendor_evidence_document_type(record: DocumentRecord, text: str) -> str | None:
    if record.upload_side != "vendor":
        return None
    haystack = f"{record.file_name}\n{text[:2000]}".lower()
    if tender_source_trap(haystack):
        return None
    if (
        "seller name" in haystack
        and "seller undertaking" in haystack
        and any(marker in haystack for marker in ["buyer specification document", "offered specification document"])
    ):
        return "gem_bid"
    if "udyam registration certificate" in haystack or "udyam number" in haystack or "enterprise name:" in haystack:
        return "mse_udyam_or_startup_exemption"
    has_pan = bool(re.search(r"\b[a-z]{5}\d{4}[a-z]\b", haystack))
    has_gstin = bool(re.search(r"\b\d{2}[a-z]{5}\d{4}[a-z][a-z0-9]z[a-z0-9]\b", haystack))
    if has_gstin or (has_pan and any(marker in haystack for marker in ["pan", "permanent account number"])):
        return "gst_pan_cin_statutory"
    if any(marker in haystack for marker in ["gst registration certificate", "form gst reg-06"]):
        return "gst_pan_cin_statutory"
    if re.search(r"\bpqr\b", haystack) and any(
        marker in haystack
        for marker in ["pre-qualification", "pre qualification", "prequalification", "point wise", "point-wise"]
    ):
        return "eligibility_pqc"
    if (
        "generated from eoffice" in haystack
        and ("district industries centre" in haystack or "for any assistance" in haystack)
    ):
        return "other"
    if any(marker in haystack for marker in ["bid securing declaration", "bid security declaration"]):
        return "bid_security_declaration"
    if "emd payment receipt" in haystack or ("earnest money" in haystack and "utr" in haystack):
        return "emd_or_bid_security"
    if re.search(r"\budin\b", haystack) or any(
        marker in haystack
        for marker in ["chartered accountant", "ca certificate", "audited financial", "balance sheet", "profit and loss"]
    ):
        return "turnover_networth_ca_certificate"
    if (
        "tax invoice" in haystack
        or "invoice serial number" in haystack
        or ("invoice date" in haystack and any(marker in haystack for marker in ["supplier:", "original for recipient", "details of receiver"]))
    ):
        return "experience_po_completion"
    if any(marker in haystack for marker in ["service entry sheet", "final bill", "ra bill", "payment advice"]):
        return "experience_po_completion"
    if (
        any(marker in haystack for marker in ["purchase order", "work order", "letter of intent", "award of work"])
        or (
            "po date" in haystack
            and (
                re.search(r"\bp\.?\s*o\.?\s*(?:no\.?|number)\b", haystack)
                or any(marker in haystack for marker in ["supp code", "ord-type", "gem po no", "supplier:"])
            )
        )
    ):
        return "experience_po_completion"
    if "completion certificate" in haystack or ("completed supply" in haystack and "executed value" in haystack):
        return "experience_po_completion"
    commercial_terms = [
        "delivery period",
        "penalty for late delivery",
        "payment terms",
        "insurance to be",
        "offer validity",
        "pre-inspection",
    ]
    if (
        any(marker in haystack for marker in ["techno-commercial bid", "techno commercial bid", "deviation to spec"])
        or sum(marker in haystack for marker in commercial_terms) >= 2
    ):
        return "techno_commercial_compliance"
    if "corporate purchasing specification" in haystack or (
        "doc rev no" in haystack and any(marker in haystack for marker in ["acceptance criteria", "type tests", "technical specification"])
    ):
        return "technical_specs_or_sow"
    if any(marker in haystack for marker in ["technical data sheet", "property specification", "nominal thickness", "product catalogue"]):
        return "datasheet_catalogue"
    if "oem authorization" in haystack or ("authorization letter" in haystack and "authorized bidder" in haystack):
        return "oem_authorization"
    if "local content certificate" in haystack or ("supplier class" in haystack and "local supplier" in haystack):
        return "mii_local_content"
    if any(marker in haystack for marker in ["country sharing land border", "land border with india", "rule 144 xi"]):
        return "land_border_declaration"
    if "integrity pact" in haystack or "independent external monitor" in haystack:
        return "integrity_pact"
    if any(marker in haystack for marker in ["blacklisting and holiday declaration", "not been blacklisted", "not blacklisted", "not debarred"]):
        return "non_blacklisting_declaration"
    if "nil deviation declaration" in haystack or "no deviation certificate" in haystack:
        return "nil_deviation_declaration"
    if "authorized signatory" in haystack and any(
        marker in haystack for marker in ["bid declaration", "bid form", "tender acceptance"]
    ):
        return "forms_annexures"
    if any(marker in haystack for marker in ["epbg", "performance bank guarantee", "bank guarantee no", "bg no"]):
        return "emd_or_bid_security"
    return None


def tender_source_trap(text: str) -> bool:
    normalized = normalize_for_match(text)
    if reference_source_material(normalized) and not bidder_filled_evidence_marker(normalized):
        return True
    return (
        "tender extract" in normalized
        or "this page repeats the tender condition" in normalized
        or ("mandatory criterion" in normalized and "required evidence" in normalized and "bidder:" not in normalized)
    )


def reference_source_material(text: str) -> bool:
    source_markers = [
        "general purchase condition",
        "gpc_doc",
        "tender terms",
        "pre-qualification criteria",
        "bid evaluation criteria",
        "rejection criteria",
        "instructions to bidders",
        "notice inviting tender",
        "buyer added bid specific",
        "gem bid document",
        "the bidder shall submit",
        "the bidder should have executed",
        "bids are liable for rejection",
        "mrpl reserves the right",
        "copy of purchase order along with any one",
    ]
    return any(marker in text for marker in source_markers)


def bidder_filled_evidence_marker(text: str) -> bool:
    filled_markers = [
        "we hereby certify",
        "we hereby declare",
        "we declare that",
        "we confirm that",
        "we have read",
        "certify that bidder",
        "name of authorized signatory",
        "name of above authorized signatory",
        "gstin",
        "udyam registration certificate",
        "udyam number",
        "tax invoice no",
        "invoice no",
        "po no",
        "purchase order no",
        "supplier:",
        "customer:",
        "executed value",
        "certificate no",
    ]
    return any(marker in text for marker in filled_markers)


TENDER_PAGE_SIGNAL_TERMS = [
    "annexure",
    "checklist",
    "documents to be submitted",
    "bidder shall submit",
    "bidder should submit",
    "must submit",
    "to be uploaded",
    "technical bid",
    "commercial bid",
    "eligibility",
    "tender document",
    "notice inviting tender",
    "request for proposal",
    "bid document",
    "qualification criteria",
    "evaluation criteria",
    "scope of work",
    "payment terms",
    "corrigendum",
    "addendum",
    "pre-bid",
    "earnest money",
    "bid security",
    "technical score",
    "cut off",
    "cut-off",
    "marks",
    "implementation",
    "project timeline",
    "declaration",
    "undertaking",
    "certificate",
    "blacklisted",
    "land border",
    "local content",
]
TENDER_LLM_HIGH_SIGNAL_TERMS = {
    "annexure",
    "checklist",
    "documents to be submitted",
    "bidder shall submit",
    "bidder should submit",
    "must submit",
    "to be uploaded",
    "technical bid",
    "commercial bid",
    "financial bid",
    "price bid",
    "eligibility",
    "tender document",
    "notice inviting tender",
    "request for proposal",
    "bid document",
    "qualification criteria",
    "evaluation criteria",
    "corrigendum",
    "bid security",
    "technical score",
    "cut off",
    "cut-off",
    "marks",
    "implementation",
    "project timeline",
    "declaration",
    "undertaking",
    "blacklisted",
    "land border",
    "local content",
}


def tender_page_signals(text: str) -> list[str]:
    normalized = normalize_for_match(text)
    return [term for term in TENDER_PAGE_SIGNAL_TERMS if term in normalized]


def page_has_bidder_submission_signal(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(
        term in normalized
        for term in [
            "documents to be submitted",
            "to be submitted",
            "bidder shall submit",
            "bidder should submit",
            "must submit",
            "to be uploaded",
            "upload",
            "technical bid submission",
            "commercial bid format",
            "bid form",
            "declaration",
            "undertaking",
            "certificate",
            "checklist",
        ]
    )


def page_has_mandatory_submission_signal(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(term in normalized for term in ["shall submit", "must submit", "mandatory", "to be submitted", "required"])


def heuristic_page_classification(page: PageUnit) -> DocumentClassification | None:
    boundary = tender_page_boundary_classification(page)
    if boundary:
        return boundary
    probe = ExtractedDocument(
        DocumentRecord(
            document_id=page.document_id,
            file_name=page.file_name,
            upload_side=page.upload_side,
            extraction_status="ok",
        ),
        page.text,
        [page],
    )
    result = heuristic_classification(probe)
    if result:
        result.title = result.title or f"{page.file_name} page {page.page_number}"
    return result


def tender_page_boundary_classification(page: PageUnit) -> DocumentClassification | None:
    if page.upload_side != "tender":
        return None
    heading_lines = [
        normalize_for_match(line)
        for line in (page.layout_text or page.text).splitlines()[:24]
        if normalize_for_match(line)
    ]
    if not heading_lines:
        return None
    corrigendum_heading = next(
        (line for line in heading_lines[:12] if re.match(
            r"^(?:(?:corrigendum(?:\s*(?:/|cum)\s*addendum)?|addendum)"
            r"(?:\s+(?:(?:no\.?|number|#)?\s*[-:#]?\s*\d+\b.*|(?:dated|to|for)\b.*))?"
            r"|(?:amendment\s+to\s+(?:rfp|tender)|tender\s+amendment)\b.*)$",
            line,
        )),
        "",
    )
    if corrigendum_heading:
        corrigendum_heading = re.sub(r"\s+page\s+\d+\s+of\s+\d+\s*$", "", corrigendum_heading).strip()
        return DocumentClassification(
            content_role="tender_source",
            document_type="corrigendum",
            title=corrigendum_heading,
            confidence=0.95,
            reason="Strong page boundary: corrigendum/addendum heading.",
        )
    prebid_heading = next(
        (
            line
            for line in heading_lines[:12]
            if re.match(
                r"^(?:pre[- ]?bid clarification|pre[- ]?bid queries|"
                r"pre[- ]?bid query reply|reply to pre[- ]?bid queries|"
                r"response to pre[- ]?bid queries)\b",
                line,
            )
        ),
        "",
    )
    if prebid_heading:
        return DocumentClassification(
            content_role="tender_source",
            document_type="prebid_clarification",
            title=prebid_heading,
            confidence=0.95,
            reason="Strong page boundary: pre-bid clarification heading.",
        )
    rfp_heading = next(
        (line for line in heading_lines[:12] if re.match(
            r"^(?:request for proposal|rfp for\b|notice inviting tender|invitation for bids|tender document(?:\s+for\b|$))",
            line,
        )),
        "",
    )
    if rfp_heading:
        return DocumentClassification(
            content_role="tender_source",
            document_type="rfp_or_bid_document",
            title=f"{page.file_name} page {page.page_number}",
            confidence=0.9,
            reason="Strong page boundary: base RFP/tender heading.",
        )
    return None


def stabilize_tender_page_classifications(document: ExtractedDocument) -> None:
    inherited = document.record.classification or DocumentClassification(content_role="tender_source", document_type="unknown")
    current = inherited
    for page in document.pages:
        classification = page.classification or inherited
        if classification.reason.startswith("Strong page boundary:"):
            current = classification
        else:
            classification = current
        page.classification = classification


def stabilize_vendor_page_classifications(document: ExtractedDocument) -> None:
    # A run of pages forming one copied tender-specification attachment must
    # share a single content role; a partial flip splits one attachment into
    # contradictory evidence/reference documents.
    pages = document.pages
    index = 0
    while index < len(pages):
        classification = pages[index].classification
        if not classification or classification.document_type != "technical_specs_or_sow":
            index += 1
            continue
        run: list[PageUnit] = []
        while (
            index < len(pages)
            and pages[index].classification
            and pages[index].classification.document_type == "technical_specs_or_sow"
        ):
            run.append(pages[index])
            index += 1
        if not any(page.classification.content_role == "reference" for page in run):
            continue
        for page in run:
            if page.classification.content_role == "reference":
                continue
            page.classification.content_role = "reference"
            page.classification.reason = (
                f"{page.classification.reason} "
                "Unified with adjacent tender-specification reference pages."
            ).strip()


def reconstruct_logical_documents(documents: list[ExtractedDocument]) -> list[LogicalDocument]:
    for document in documents:
        document.record.logical_document_ids = []
    logical_documents: list[LogicalDocument] = []
    counter = 1
    for document in documents:
        if not document.pages:
            classification = document.record.classification or DocumentClassification(document_type="unknown")
            logical = make_logical_document(counter, document, [document.record.document_id], classification)
            document.record.logical_document_ids = [logical.logical_document_id]
            logical_documents.append(logical)
            counter += 1
            continue

        current_key: tuple[str, str, str] | None = None
        current_pages: list[PageUnit] = []
        for page in document.pages:
            classification = page.classification or document.record.classification or DocumentClassification(document_type="unknown")
            boundary_title = classification.title or "" if classification.document_type in POST_BASE_RFP_DOCUMENT_TYPES else ""
            key = (classification.content_role, classification.document_type, boundary_title)
            if current_key is not None and key != current_key:
                logical = make_logical_document(counter, document, current_pages, current_pages[0].classification or classification)
                logical_documents.append(logical)
                document.record.logical_document_ids.append(logical.logical_document_id)
                counter += 1
                current_pages = []
            current_key = key
            current_pages.append(page)
        if current_pages:
            classification = current_pages[0].classification or document.record.classification or DocumentClassification(document_type="unknown")
            logical = make_logical_document(counter, document, current_pages, classification)
            logical_documents.append(logical)
            document.record.logical_document_ids.append(logical.logical_document_id)
            counter += 1
    return logical_documents


def _validate_document_regroups(data: Any, page_count: int) -> list[dict[str, Any]] | None:
    """Deterministically validate an LLM page-grouping: every page exactly
    once, ascending, contiguous groups, known document types."""
    documents = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(documents, list) or not documents:
        return None
    groups: list[dict[str, Any]] = []
    expected = 1
    for item in documents:
        if not isinstance(item, dict):
            return None
        pages = item.get("pages")
        document_type = str(item.get("document_type") or "")
        title = str(item.get("title") or "").strip()
        if not isinstance(pages, list) or not pages or document_type not in DOC_TYPES:
            return None
        try:
            numbers = [int(number) for number in pages]
        except (TypeError, ValueError):
            return None
        if numbers[0] != expected or numbers != list(range(numbers[0], numbers[-1] + 1)):
            return None
        expected = numbers[-1] + 1
        groups.append({"title": title, "document_type": document_type, "pages": numbers})
    if expected != page_count + 1:
        return None
    return groups


def classify_documents(
    documents: list[ExtractedDocument],
    *,
    allow_llm: bool = True,
) -> None:
    for document in documents:
        if document.record.input_role == "evaluation_guideline":
            document.record.classification = DocumentClassification(
                content_role="reference",
                document_type="unknown",
                title=document.record.file_name,
                confidence=1,
                reason="Evaluation guideline supplied outside the tender packet.",
            )
            continue
        if not document.text:
            document.record.classification = DocumentClassification(
                document_type="unknown",
                confidence=0,
                reason="No extractable text.",
            )
            continue
        heuristic = heuristic_classification(document)
        if heuristic:
            document.record.classification = heuristic
            continue
        if not allow_llm:
            document.record.classification = DocumentClassification(
                content_role=default_content_role(document.record.upload_side),
                document_type=(
                    "rfp_or_bid_document"
                    if document.record.upload_side == "tender"
                    else "unknown"
                ),
                title=document.record.file_name,
                confidence=0.35,
                reason="Generic local fallback; no model classification was requested.",
            )
            continue
        data = llm_runtime.llm_json(
            prompt_text(
                "document_classifier",
                "You classify Indian tender and bidder-submission documents. Return only JSON.",
            ),
            {
                "task": "classify_document",
                "allowed_document_types": DOC_TYPES,
                "allowed_content_roles": ["tender_source", "vendor_submission", "reference", "unknown"],
                "upload_side": document.record.upload_side,
                "classification_rules": VENDOR_CLASSIFICATION_RULES,
                "file_name": document.record.file_name,
                "text": document.text[:LLM_CLASSIFY_CHARS],
                "json_shape": {
                    "content_role": "tender_source|vendor_submission|reference|unknown",
                    "document_type": "one allowed_document_types value",
                    "title": "short title or null",
                    "confidence": "0 to 1",
                    "reason": "short reason",
                    "key_facts": ["important facts found"],
                },
            },
        )
        document.record.classification = normalize_classification(
            document.record,
            document.text,
            validate_model(
                DocumentClassification,
                coerce_classification_data(data, default_content_role(document.record.upload_side)),
            ),
        )


def classify_pages(
    documents: list[ExtractedDocument],
    *,
    allow_llm: bool = True,
) -> None:
    llm_candidates: list[tuple[PageUnit, DocumentClassification | None]] = []
    for document in documents:
        if document.record.input_role == "evaluation_guideline":
            for page in document.pages:
                page.classification = document.record.classification
            continue
        for page in document.pages:
            if not page.text:
                page.classification = DocumentClassification(
                    content_role=(
                        document.record.classification.content_role
                        if document.record.classification
                        else "unknown"
                    ),
                    document_type="unknown",
                    confidence=0,
                    reason="No extractable page text.",
                )
                continue
            if page.ocr_required and (
                page.quality_metrics.get("native_text_integrity_gate")
                or page.quality_metrics.get("repeated_boilerplate_only")
            ):
                page.classification = DocumentClassification(
                    content_role=default_content_role(page.upload_side),
                    document_type="unknown",
                    confidence=0,
                    reason="Page content remains unreadable after required OCR; human review is required.",
                )
                continue
            heuristic = heuristic_page_classification(page)
            if heuristic:
                page.classification = heuristic
                continue
            if (
                document.record.upload_side == "tender"
                and document.record.classification
                and document.record.classification.content_role == "tender_source"
            ):
                page.classification = document.record.classification
                continue
            if len(llm_candidates) >= MAX_LLM_PAGE_CLASSIFICATIONS or not allow_llm:
                page.classification = document.record.classification
                continue
            llm_candidates.append((page, document.record.classification))

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def classify_one(item: tuple[PageUnit, DocumentClassification | None]) -> None:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        page, document_classification = item
        try:
            data = llm_runtime.llm_json(
                prompt_text(
                    "page_classifier",
                    "You classify one page from an Indian procurement tender or bid packet. Return only JSON.",
                ),
                {
                    "task": "classify_page",
                    "allowed_document_types": DOC_TYPES,
                    "allowed_content_roles": ["tender_source", "vendor_submission", "reference", "unknown"],
                    "classification_rules": VENDOR_CLASSIFICATION_RULES,
                    "upload_side": page.upload_side,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "text": page.text[:LLM_CLASSIFY_CHARS],
                    "json_shape": {
                        "content_role": "tender_source|vendor_submission|reference|unknown",
                        "document_type": "one allowed_document_types value",
                        "title": "short page/document title or null",
                        "confidence": "0 to 1",
                        "reason": "short reason",
                        "key_facts": ["important facts found"],
                    },
                },
            )
            page.classification = normalize_classification(
                DocumentRecord(
                    document_id=page.document_id,
                    file_name=page.file_name,
                    upload_side=page.upload_side,
                    extraction_status="ok",
                ),
                page.text,
                validate_model(
                    DocumentClassification,
                    coerce_classification_data(data, default_content_role(page.upload_side)),
                ),
            )
        except Exception:
            page.classification = document_classification
            raise

    if BID_CLASSIFICATION_CONCURRENCY == 1 or len(llm_candidates) <= 1:
        for item in llm_candidates:
            classify_one(item)
    else:
        with ThreadPoolExecutor(
            max_workers=min(BID_CLASSIFICATION_CONCURRENCY, len(llm_candidates))
        ) as executor:
            futures = [executor.submit(classify_one, item) for item in llm_candidates]
            for future in as_completed(futures):
                future.result()

    for document in documents:
        if document.record.upload_side == "tender":
            stabilize_tender_page_classifications(document)
        else:
            stabilize_vendor_page_classifications(document)


def regroup_logical_documents(
    documents: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
) -> list[LogicalDocument]:
    """Semantically join consecutive pages that form one bidder document."""
    if llm_runtime.PROCUREMENT_OFFLINE_LLM:
        return logical_documents
    regrouped: list[LogicalDocument] = []
    counter = 1
    for document in documents:
        pages = [page for page in document.pages if page.text.strip()]
        owned = [
            logical
            for logical in logical_documents
            if document.record.document_id in logical.source_document_ids
        ]
        if len(pages) < 2 or not owned:
            regrouped.extend(owned)
            counter += len(owned)
            continue
        type_by_page = {
            ref: logical.document_type
            for logical in owned
            for ref in logical.page_refs
        }
        manifest = [
            {
                "page": page.page_number,
                "current_type": type_by_page.get(page.page_id, "unknown"),
                "start": clean_text((page.layout_text or page.text)[:220]),
            }
            for page in pages
        ]
        try:
            data = llm_runtime.llm_json(
                prompt_text(
                    "bid_document_regroup",
                    "You segment a bidder submission PDF into its logical documents. Return only JSON.",
                ),
                {
                    "task": "bid_document_regroup",
                    "rules": [
                        "Group consecutive pages into the logical documents they form; one document often spans several pages.",
                        "Every page must appear exactly once, in ascending order; groups must be contiguous.",
                        "A page that continues the previous document belongs to the previous group.",
                        "Choose the allowed document_type that best matches each group and give a short title.",
                    ],
                    "allowed_document_types": DOC_TYPES,
                    "pages": manifest,
                    "json_shape": {
                        "documents": [
                            {
                                "title": "short meaningful title",
                                "document_type": "one allowed value",
                                "pages": [1, 2],
                            }
                        ]
                    },
                },
                max_output_tokens=4096,
                thinking_budget=0,
            )
            groups = _validate_document_regroups(data, len(pages))
        except Exception:
            groups = None
        if not groups:
            regrouped.extend(owned)
            counter += len(owned)
            continue
        page_by_number = {page.page_number: page for page in pages}
        for group in groups:
            group_pages = [page_by_number[number] for number in group["pages"]]
            first_classification = group_pages[0].classification
            classification = DocumentClassification(
                content_role=(
                    first_classification.content_role
                    if first_classification
                    else "vendor_submission"
                ),
                document_type=group["document_type"],
                title=group["title"] or None,
                confidence=0.8,
                reason="Semantic page grouping across document boundaries.",
            )
            logical = make_logical_document(counter, document, group_pages, classification)
            if group["title"]:
                numbers = group["pages"]
                logical.title = (
                    group["title"]
                    if len(numbers) == 1
                    else f"{group['title']} (pages {numbers[0]}-{numbers[-1]})"
                )
            regrouped.append(logical)
            counter += 1
    return regrouped


def summarize_logical_documents(
    logical_documents: list[LogicalDocument],
    documents: list[ExtractedDocument],
) -> None:
    """Add concise routing metadata; source pages remain the only evidence."""
    pages_by_id = {
        page.page_id: page
        for document in documents
        for page in document.pages
    }
    pending = [
        logical
        for logical in logical_documents
        if logical.upload_side == "vendor" and not logical.content_summary
    ]
    batches: list[list[tuple[LogicalDocument, str]]] = []
    batch: list[tuple[LogicalDocument, str]] = []
    batch_chars = 0
    for logical in pending:
        pages = [pages_by_id[page_id] for page_id in logical.page_refs if page_id in pages_by_id]
        per_page = max(250, BID_SUMMARY_DOCUMENT_CHARS // max(1, len(pages)))
        text = clean_text(
            "\n".join(
                f"[page {page.page_number}] {page.text[:per_page]}"
                for page in pages
                if page.text
            )
        )[:BID_SUMMARY_DOCUMENT_CHARS]
        if batch and (
            len(batch) >= BID_SUMMARY_BATCH_DOCUMENTS
            or batch_chars + len(text) > BID_SUMMARY_BATCH_CHARS
        ):
            batches.append(batch)
            batch = []
            batch_chars = 0
        batch.append((logical, text))
        batch_chars += len(text)
    if batch:
        batches.append(batch)

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def summarize_batch(items: list[tuple[LogicalDocument, str]]) -> None:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        try:
            data = llm_runtime.llm_json(
                prompt_text(
                    "bid_document_summary",
                    "You summarize classified bidder documents for procurement evidence routing. Return only JSON.",
                ),
                {
                    "task": "summarize_bid_documents",
                    "rules": [
                        "Return exactly one summary for every logical_document_id.",
                        "Describe only what the bidder document contains; never decide whether a tender criterion passes.",
                        "Mention project experience, customers, services, products, technologies, certificates, identifiers, values, and dates when present.",
                        "Keep content_summary within 80 words and return at most 8 source-grounded key_facts.",
                    ],
                    "documents": [
                        {
                            "logical_document_id": logical.logical_document_id,
                            "title": logical.title,
                            "document_type": logical.document_type,
                            "text": text,
                        }
                        for logical, text in items
                    ],
                    "json_shape": {
                        "summaries": [
                            {
                                "logical_document_id": "one supplied id",
                                "content_summary": "concise factual description",
                                "key_facts": ["specific searchable facts"],
                            }
                        ]
                    },
                },
                max_output_tokens=max(4096, len(items) * 768),
                thinking_budget=0,
            )
            by_id = {
                str(item.get("logical_document_id") or ""): item
                for item in (data.get("summaries") or [])
                if isinstance(item, dict)
            }
            missing = [
                logical.logical_document_id
                for logical, _text in items
                if logical.logical_document_id not in by_id
            ]
            if missing:
                raise ValueError(
                    f"bid document summary provider omitted {len(missing)} classified document(s)"
                )
            for logical, _text in items:
                summary = by_id[logical.logical_document_id]
                logical.content_summary = clean_text(str(summary.get("content_summary") or ""))[:2000]
                logical.key_facts = unique_items(
                    [
                        clean_text(str(fact))[:500]
                        for fact in (summary.get("key_facts") or [])
                        if clean_text(str(fact))
                    ]
                )[:20]
                if not logical.content_summary:
                    raise ValueError(
                        f"bid document summary provider returned an empty summary for {logical.logical_document_id}"
                    )
        except (llm_runtime.GeminiOutputTruncated, json.JSONDecodeError, ValueError) as exc:
            if len(items) > 1:
                midpoint = len(items) // 2
                summarize_batch(items[:midpoint])
                summarize_batch(items[midpoint:])
                return
            logical, text = items[0]
            logical.content_summary = clean_text(text)[:2000] or logical.title
            logical.key_facts = []
            logger.warning(
                "Using extracted-text summary for %s after invalid provider JSON: %s",
                logical.logical_document_id,
                exc,
            )

    if BID_CLASSIFICATION_CONCURRENCY == 1 or len(batches) <= 1:
        for items in batches:
            summarize_batch(items)
    else:
        with ThreadPoolExecutor(
            max_workers=min(BID_CLASSIFICATION_CONCURRENCY, len(batches))
        ) as executor:
            futures = [executor.submit(summarize_batch, items) for items in batches]
            for future in as_completed(futures):
                future.result()
