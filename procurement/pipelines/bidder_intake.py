"""Bidder-intake pipeline for submission ingestion and classification."""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from procurement.domain.serialization import model_dump as _default_model_dump
from procurement.evaluation.index import (
    artifact_labels_and_capabilities,
    build_bidder_index_manifest,
)
from procurement.evaluation.semantic_retrieval import bidder_semantic_index
from procurement.platform.ingestion import unpack_upload
from procurement.understanding.classification import (
    classify_documents as _default_classify_documents,
    classify_pages as _default_classify_pages,
    reconstruct_logical_documents as _default_reconstruct_logical_documents,
    regroup_logical_documents as _default_regroup_logical_documents,
    summarize_logical_documents as _default_summarize_logical_documents,
)
from procurement.understanding.documents import load_upload_documents as _default_load_upload_documents
from procurement.understanding.ocr_pipeline import OCR_RESULT_CACHE_DIR, run_base_rfp_ocr as _default_run_base_rfp_ocr

from .support import (
    _bid_tender_match,
    _ocr_gap_message,
    _raise_on_ocr_errors,
    _store_preview_sources as _default_store_preview_sources,
    commercial_quarantine,
)

logger = logging.getLogger("procurement.worker")


@dataclass(frozen=True)
class BidderIntakeHooks:
    """Optional boundary callbacks retained for tests and compatibility callers."""

    load_upload_documents: Callable[..., Any] | None = None
    run_base_rfp_ocr: Callable[..., Any] | None = None
    classify_documents: Callable[..., Any] | None = None
    classify_pages: Callable[..., Any] | None = None
    reconstruct_logical_documents: Callable[..., Any] | None = None
    regroup_logical_documents: Callable[..., Any] | None = None
    summarize_logical_documents: Callable[..., Any] | None = None
    model_dump: Callable[..., Any] | None = None
    store_preview_sources: Callable[..., Any] | None = None


def intake_bid(
    bid_bundle: Path,
    work: Path,
    *,
    understanding: dict[str, Any] | None = None,
    preview_context: tuple[str, str] | None = None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
    hooks: BidderIntakeHooks | None = None,
) -> dict:
    hooks = hooks or BidderIntakeHooks()
    load_upload_documents = hooks.load_upload_documents or _default_load_upload_documents
    run_base_rfp_ocr = hooks.run_base_rfp_ocr or _default_run_base_rfp_ocr
    classify_documents = hooks.classify_documents or _default_classify_documents
    classify_pages = hooks.classify_pages or _default_classify_pages
    reconstruct_logical_documents = hooks.reconstruct_logical_documents or _default_reconstruct_logical_documents
    regroup_logical_documents = hooks.regroup_logical_documents or _default_regroup_logical_documents
    summarize_logical_documents = hooks.summarize_logical_documents or _default_summarize_logical_documents
    model_dump = hooks.model_dump or _default_model_dump
    _store_preview_sources = hooks.store_preview_sources or _default_store_preview_sources
    documents = load_upload_documents(
        bid_bundle,
        work,
        "vendor",
        input_role="vendor_submission",
        document_id_prefix="vendor",
    )
    if progress:
        progress(
            "native_text_complete",
            {
                "documents": len(documents),
                "pages": sum(len(document.pages) for document in documents),
                "scanned_pages": sum(
                    bool(page.ocr_required)
                    for document in documents
                    for page in document.pages
                ),
            },
        )
        progress(
            "ocr_started",
            {
                "provider": os.getenv("BASE_RFP_OCR_PROVIDER", "paddle"),
                "pages": sum(
                    bool(page.ocr_required)
                    for document in documents
                    for page in document.pages
                ),
            },
        )
    ocr_notes = run_base_rfp_ocr(documents, OCR_RESULT_CACHE_DIR / "layout")
    ocr_errors = [note for note in ocr_notes if note.get("status") == "error"]
    if ocr_errors:
        logger.warning(
            "Bidder OCR incomplete failed_pages=%s; classification will require review",
            len(ocr_errors),
        )
    _raise_on_ocr_errors(ocr_notes, "Bidder")
    if progress:
        progress(
            "ocr_complete",
            {
                "processed_pages": len(ocr_notes),
                "cache_hits": sum(bool(note.get("cache_hit")) for note in ocr_notes),
            },
        )
    preview_sources: dict[str, dict[str, str]] = {}
    preview_warning = None
    if preview_context:
        try:
            preview_sources = _store_preview_sources(
                documents,
                *preview_context,
                storage_group="bid-source-previews",
            )
        except Exception as exc:
            preview_warning = (
                "Bidder document previews could not be prepared; classification continued "
                "and the preview service should be retried."
            )
            logger.warning("Bidder preview preparation failed: %s", type(exc).__name__)
    if progress:
        progress(
            "llm_classification_started",
            {
                "provider": os.getenv("AI_PROVIDER", "gemini"),
                "model": os.getenv("AI_MODEL_FAST") or os.getenv("GEMINI_MODEL_FAST", ""),
            },
        )
    classify_documents(documents)
    classify_pages(documents)
    logical_documents = reconstruct_logical_documents(documents)
    logical_documents = regroup_logical_documents(documents, logical_documents)
    summarize_logical_documents(logical_documents, documents)
    page_by_id = {
        page.page_id: page
        for document in documents
        for page in document.pages
    }
    for logical_document in logical_documents:
        labels, capabilities = artifact_labels_and_capabilities(
            document_type=logical_document.document_type,
            title=logical_document.title,
            summary=logical_document.content_summary,
            key_facts=logical_document.key_facts,
            text="\n".join(
                page_by_id[page_id].text
                for page_id in logical_document.page_refs
                if page_id in page_by_id
            ),
        )
        logical_document.labels = labels
        logical_document.capabilities = capabilities
    for document in documents:
        document_text = "\n".join(page.text for page in document.pages)
        classification = (
            document.record.get("classification")
            if isinstance(document.record, dict)
            else document.record.classification
        )
        if classification:
            get = classification.get if isinstance(classification, dict) else lambda key, default=None: getattr(classification, key, default)
            labels, capabilities = artifact_labels_and_capabilities(
                document_type=str(get("document_type", "unknown") or "unknown"),
                title=str(
                    get("title", "")
                    or (document.record.get("file_name", "") if isinstance(document.record, dict) else document.record.file_name)
                ),
                key_facts=get("key_facts", []) or [],
                text=document_text,
            )
            if isinstance(classification, dict):
                classification["labels"] = labels
                classification["capabilities"] = capabilities
            else:
                classification.labels = labels
                classification.capabilities = capabilities
        for page in document.pages:
            if not page.classification:
                continue
            labels, capabilities = artifact_labels_and_capabilities(
                document_type=page.classification.document_type,
                title=page.classification.title or page.file_name,
                key_facts=page.classification.key_facts,
                text=page.text,
            )
            page.classification.labels = labels
            page.classification.capabilities = capabilities
    all_pages = [page for document in documents for page in document.pages]
    search_index = build_bidder_index_manifest(logical_documents, all_pages)
    prewarmed = bidder_semantic_index([page for page in all_pages if page.text.strip()])
    search_index["embedding_prewarm"] = {
        "status": "ready" if prewarmed else "unavailable",
        "pages": int((prewarmed or {}).get("pages") or 0),
        "chunks": len((prewarmed or {}).get("entries") or []),
    }
    tender_match = _bid_tender_match(understanding or {}, documents)
    if progress:
        progress(
            "llm_classification_complete",
            {"recognized_documents": len(logical_documents)},
        )
    inventory = [model_dump(item.record) for item in documents]
    return {
        "documents": inventory,
        "preview_sources": preview_sources,
        "layout_ocr": ocr_notes,
        "pages": [model_dump(page) for item in documents for page in item.pages],
        "tender_match": tender_match,
        "classification": {
            "ai_proposal": [model_dump(item) for item in logical_documents],
            "logical_documents": [model_dump(item) for item in logical_documents],
        },
        "search_index": search_index,
        "warnings": [
            warning
            for warning in (_ocr_gap_message(ocr_notes, "Bidder"), preview_warning)
            if warning
        ],
    }


def technical_bid_bundle(bundle: Path, intake: dict, work: Path) -> tuple[Path, int]:
    if os.getenv("COMMERCIAL_OPENING_ENABLED", "0").strip() == "1":
        return bundle, 0
    documents = list(intake.get("documents") or [])
    quarantine = commercial_quarantine(intake)
    keep = quarantine["keep_document_ids"]

    def withheld(item: dict) -> bool:
        return str(item.get("document_id") or "") not in keep

    if bundle.suffix.lower() != ".zip":
        if documents and withheld(documents[0]):
            raise ValueError(
                "This bid was classified as price-only. Move technical sections off Price Bid, then evaluate again."
            )
        return bundle, 0

    unpacked_root = work / "unpacked"
    extracted = unpack_upload(bundle, unpacked_root)
    allowed = [
        path
        for index, path in enumerate(extracted)
        if index >= len(documents) or not withheld(documents[index])
    ]
    if not allowed:
        raise ValueError(
            "This bid was classified as price-only. Move technical sections off Price Bid, then evaluate again."
        )
    output = work / "technical-submission.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in allowed:
            archive.write(path, arcname=path.relative_to(unpacked_root).as_posix())
    return output, len(extracted) - len(allowed)


__all__ = ["BidderIntakeHooks", "intake_bid", "technical_bid_bundle"]
