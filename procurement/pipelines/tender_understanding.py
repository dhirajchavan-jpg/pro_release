"""Tender-understanding pipeline for schema-driven tender ingestion."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_provider import embed_texts, rerank_texts, warmup_llm
from procurement.domain.serialization import model_dump as _default_model_dump
from procurement.platform.legacy_jobs import artifact_dir_for as _default_artifact_dir_for
from procurement.platform.provider_usage import bind_provider_usage, register_provider_thread
from procurement.understanding.classification import (
    classify_documents as _default_classify_documents,
    classify_pages as _default_classify_pages,
    reconstruct_logical_documents as _default_reconstruct_logical_documents,
)
from procurement.understanding.documents import load_upload_documents as _default_load_upload_documents
from procurement.understanding.ocr_pipeline import OCR_RESULT_CACHE_DIR, run_base_rfp_ocr as _default_run_base_rfp_ocr
from procurement.understanding.service import (
    profile_from_fields as _default_profile_from_fields,
    requirement_matrix_from_fields as _default_requirement_matrix_from_fields,
    run_field_understanding as _default_run_field_understanding,
)
from procurement.understanding.sources import identify_base_rfp_set as _default_identify_base_rfp_set
from procurement.understanding.structure import reconstruct_document_structure as _default_reconstruct_document_structure
from procurement.understanding.structural_review import (
    BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED,
    run_structural_adjudication as _default_run_structural_adjudication,
)

from .support import (
    _ocr_gap_message,
    _raise_on_ocr_errors,
    _store_preview_sources as _default_store_preview_sources,
)

logger = logging.getLogger("procurement.worker")


@dataclass(frozen=True)
class TenderUnderstandingHooks:
    """Optional boundary callbacks retained for tests and compatibility callers."""

    load_upload_documents: Callable[..., Any] | None = None
    run_base_rfp_ocr: Callable[..., Any] | None = None
    classify_documents: Callable[..., Any] | None = None
    classify_pages: Callable[..., Any] | None = None
    reconstruct_logical_documents: Callable[..., Any] | None = None
    reconstruct_document_structure: Callable[..., Any] | None = None
    identify_base_rfp_set: Callable[..., Any] | None = None
    run_structural_adjudication: Callable[..., Any] | None = None
    artifact_dir_for: Callable[..., Any] | None = None
    model_dump: Callable[..., Any] | None = None
    store_preview_sources: Callable[..., Any] | None = None
    profile_from_fields: Callable[..., Any] | None = None
    requirement_matrix_from_fields: Callable[..., Any] | None = None
    run_field_understanding: Callable[..., Any] | None = None
    structural_adjudication_enabled: bool | None = None


def understand_tender(
    operation_id: str,
    tender_bundle: Path,
    work: Path,
    field_schema: dict,
    *,
    preview_context: tuple[str, str] | None = None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
    hooks: TenderUnderstandingHooks | None = None,
) -> dict:
    """Run schema-driven tender understanding without exhaustive atomization."""
    hooks = hooks or TenderUnderstandingHooks()
    load_upload_documents = hooks.load_upload_documents or _default_load_upload_documents
    run_base_rfp_ocr = hooks.run_base_rfp_ocr or _default_run_base_rfp_ocr
    classify_documents = hooks.classify_documents or _default_classify_documents
    classify_pages = hooks.classify_pages or _default_classify_pages
    reconstruct_logical_documents = hooks.reconstruct_logical_documents or _default_reconstruct_logical_documents
    reconstruct_document_structure = hooks.reconstruct_document_structure or _default_reconstruct_document_structure
    identify_base_rfp_set = hooks.identify_base_rfp_set or _default_identify_base_rfp_set
    run_structural_adjudication = hooks.run_structural_adjudication or _default_run_structural_adjudication
    artifact_dir_for = hooks.artifact_dir_for or _default_artifact_dir_for
    model_dump = hooks.model_dump or _default_model_dump
    _store_preview_sources = hooks.store_preview_sources or _default_store_preview_sources
    profile_from_fields = hooks.profile_from_fields or _default_profile_from_fields
    requirement_matrix_from_fields = hooks.requirement_matrix_from_fields or _default_requirement_matrix_from_fields
    run_field_understanding = hooks.run_field_understanding or _default_run_field_understanding
    structural_adjudication_enabled = (
        BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED
        if hooks.structural_adjudication_enabled is None
        else hooks.structural_adjudication_enabled
    )

    def warm_llm() -> None:
        bind_provider_usage(operation_id)
        try:
            warmup_llm(model=os.getenv("AI_MODEL_DEEP") or None)
        except Exception as exc:
            logger.warning(
                "Early LLM warmup did not complete operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )

    def warm_reranker() -> None:
        bind_provider_usage(operation_id)
        if os.getenv("RFP_FIELD_RERANKER_ENABLED", "0") != "1":
            return
        try:
            rerank_texts(
                "procurement tender field relevance",
                ["procurement tender field relevance"],
                top_n=1,
            )
        except Exception as exc:
            logger.warning(
                "Early reranker warmup did not complete operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )

    def warm_embedding() -> None:
        bind_provider_usage(operation_id)
        if os.getenv("RFP_FIELD_EMBEDDING_ENABLED", "0") != "1":
            return
        try:
            embed_texts(["procurement tender field relevance"])
        except Exception as exc:
            logger.warning(
                "Early embedding warmup did not complete operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )

    warmup_threads: list[threading.Thread] = []
    if os.getenv("AI_UNDERSTANDING_EARLY_WARMUP_ENABLED", "0") == "1":
        warmup_threads = [
            threading.Thread(
                target=warm_llm,
                name=f"llm-warmup-{operation_id}",
            ),
            threading.Thread(
                target=warm_reranker,
                name=f"reranker-warmup-{operation_id}",
            ),
            threading.Thread(
                target=warm_embedding,
                name=f"embedding-warmup-{operation_id}",
            ),
        ]
        for thread in warmup_threads:
            register_provider_thread(operation_id, thread)
            thread.start()

    documents = load_upload_documents(
        tender_bundle,
        work / "tender",
        "tender",
        input_role="tender_packet",
        document_id_prefix="tender",
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
    preview_sources: dict[str, dict[str, str]] = {}
    preview_warning = None
    if preview_context:
        try:
            preview_sources = _store_preview_sources(documents, *preview_context)
        except Exception as exc:
            preview_warning = (
                "Document previews could not be prepared; extraction continued and the "
                "preview service should be retried."
            )
            logger.warning("Tender preview preparation failed: %s", type(exc).__name__)
    checkpoint_root = artifact_dir_for(operation_id) / "checkpoints"
    if progress:
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
            "Tender OCR incomplete operation_id=%s failed_pages=%s; continuing with readable pages",
            operation_id,
            len(ocr_errors),
        )
    _raise_on_ocr_errors(ocr_notes, "Tender")
    if progress:
        progress(
            "ocr_complete",
            {
                "processed_pages": len(ocr_notes),
                "cache_hits": sum(bool(note.get("cache_hit")) for note in ocr_notes),
            },
        )
        progress("document_structure_started", {})
    classify_documents(documents, allow_llm=False)
    classify_pages(documents, allow_llm=False)
    logical_documents = reconstruct_logical_documents(documents)
    structure = reconstruct_document_structure(documents, logical_documents)
    if structural_adjudication_enabled:
        structure = run_structural_adjudication(
            documents, logical_documents, structure, checkpoint_root / "structure"
        )
    selection = identify_base_rfp_set(
        logical_documents,
        [page for document in documents for page in document.pages],
    )
    if progress:
        progress(
            "llm_understanding_started",
            {
                "provider": os.getenv("AI_PROVIDER", "gemini"),
                "model": os.getenv("AI_MODEL_DEEP") or os.getenv("GEMINI_MODEL_DEEP", ""),
                "fields": sum(
                    len(section.get("fields") or [])
                    for section in field_schema.get("sections") or []
                ),
            },
        )
    # Warmups overlap local preparation but must finish before the operation's
    # usage sink is released. The provider clients enforce their own timeouts.
    for thread in warmup_threads:
        thread.join()
    understanding, source_units = run_field_understanding(
        operation_id,
        documents,
        logical_documents,
        selection,
        field_schema,
        checkpoint_root / "field_understanding",
    )
    if progress:
        progress(
            "llm_understanding_complete",
            {"status": understanding.get("status", "unknown")},
        )
    warnings = []
    ocr_warning = _ocr_gap_message(ocr_notes, "Tender")
    if ocr_warning:
        warnings.append(ocr_warning)
    if preview_warning:
        warnings.append(preview_warning)
    if understanding.get("status") != "complete":
        warnings.append("One or more configured tender fields require human review.")
    amendment_state = understanding.get("amendment_state") or {}
    if amendment_state.get("status") not in {None, "complete"}:
        warnings.append("One or more corrigendum or clarification effects require human review.")
    for selection_item in selection:
        if selection_item.get("revision_order_warning"):
            warnings.append(str(selection_item["revision_order_warning"]))
    result = {
        "file_inventory": [model_dump(item.record) for item in documents],
        "preview_sources": preview_sources,
        "extracted_units": [model_dump(page) for item in documents for page in item.pages],
        "layout_ocr": ocr_notes,
        "document_structure": model_dump(structure),
        "base_rfp_selection": selection,
        "source_units": source_units,
        "field_understanding": understanding,
        "field_schema": field_schema,
        "tender_profile": {},
        "requirement_matrix": {},
        "warnings": warnings,
    }
    if checkpoint:
        checkpoint(result)
    result["tender_profile"] = profile_from_fields(understanding)
    result["requirement_matrix"] = requirement_matrix_from_fields(
        understanding, source_units
    )
    return result


__all__ = ["TenderUnderstandingHooks", "understand_tender"]
