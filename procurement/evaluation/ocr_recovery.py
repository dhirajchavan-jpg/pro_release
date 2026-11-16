"""Targeted OCR recovery and evidence snippet helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ai_provider import settings as ai_settings
from procurement.domain.schemas import (
    CriteriaEvidenceMap,
    CriteriaFinding,
    EvidenceMapEntry,
    EvidencePacketSet,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    RequirementMatrix,
    RequirementMatrixItem,
)
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match
from procurement.evaluation.evidence_pipeline import (
    EVIDENCE_PACKET_MAX_PAGES,
    EVIDENCE_PACKET_NEIGHBOR_PAGES,
    EVIDENCE_VERIFIER_PAGE_CHARS,
    evidence_marker_score,
)
from procurement.evaluation.facts import canonical_evidence_type
from procurement.evaluation.validators import normalize_check_key
from procurement.platform.job_context import JOB_CONTEXT, check_job_deadline
from procurement.platform.files import write_json_atomic
from procurement.platform.llm import gemini_api_key
from procurement.platform.provider_usage import (
    current_operation_id,
    fingerprint,
    record_provider_call,
)
from procurement.providers.errors import classify_provider_exception
from procurement.understanding.classification import heuristic_page_classification
from procurement.understanding.layout import normalize_page_layout_data
from procurement.understanding.ocr_pipeline import (
    BASE_RFP_OCR_PROVIDER,
    BASE_RFP_PADDLE_API_BATCH_PAGES,
    BASE_RFP_PADDLE_API_CONCURRENCY,
    BASE_RFP_PADDLE_API_RETRY_ATTEMPTS,
    OCR_CONCURRENCY,
    OCR_RESULT_CACHE_DIR,
    ocr_pdf_page_with_gemini,
)
from procurement.understanding.ocr_providers import (
    BASE_RFP_OCR_MIN_CONFIDENCE,
    BASE_RFP_PADDLE_API_MODEL,
    OPENPARSER_BASE_URL,
    OPENPARSER_OCR_MODEL,
    ocr_pdf_page_layout_with_paddle,
    ocr_pdf_pages_layout_with_openparser,
    ocr_pdf_pages_layout_with_paddle_api,
)
from procurement.understanding.quality import MAX_DOC_CHARS, page_text_quality, validate_ocr_text


logger = logging.getLogger("procurement_evaluator")
TARGETED_OCR_MAX_PAGES = int(os.getenv("TARGETED_OCR_MAX_PAGES", "18"))
EVIDENCE_RELEVANT_OCR_MAX_PAGES = int(os.getenv("EVIDENCE_RELEVANT_OCR_MAX_PAGES", "60"))
EVIDENCE_OCR_DOCUMENT_SIGNAL_PAGES = max(1, int(os.getenv("EVIDENCE_OCR_DOCUMENT_SIGNAL_PAGES", "12")))
TARGETED_OCR_ENABLED = os.getenv("TARGETED_OCR_ENABLED", "1") != "0"


def run_targeted_ocr(
    vendor_docs: list[ExtractedDocument],
    matrix: RequirementMatrix,
    evidence_map: CriteriaEvidenceMap,
    findings: list[CriteriaFinding],
    logical_documents: list[LogicalDocument],
) -> list[dict[str, Any]]:
    if not TARGETED_OCR_ENABLED or TARGETED_OCR_MAX_PAGES <= 0:
        return []
    problem_criteria = {
        finding.criterion_id
        for finding in findings
        if finding.mandatory and finding.status in {"missing", "fail", "unclear"}
    }
    if not problem_criteria:
        return []
    matrix_by_id = {item.criterion_id: item for item in matrix.items}
    entries = [
        entry
        for entry in evidence_map.entries
        if entry.required and entry.criterion_id in problem_criteria
    ]
    candidates = targeted_ocr_candidates(vendor_docs, entries, matrix_by_id, logical_documents)
    return run_ocr_candidates(
        candidates,
        TARGETED_OCR_MAX_PAGES,
        f"{BASE_RFP_OCR_PROVIDER}_targeted_ocr",
    )


def run_evidence_relevant_ocr(
    vendor_docs: list[ExtractedDocument],
    matrix: RequirementMatrix,
    evidence_map: CriteriaEvidenceMap,
    logical_documents: list[LogicalDocument],
    evidence_packets: EvidencePacketSet,
) -> list[dict[str, Any]]:
    if not TARGETED_OCR_ENABLED or EVIDENCE_RELEVANT_OCR_MAX_PAGES <= 0:
        return []
    if BASE_RFP_OCR_PROVIDER in {"gemini", "ai"} and not gemini_api_key():
        return [
            {
                "status": "skipped",
                "reason": "The configured AI provider is unavailable for evidence-relevant OCR.",
            }
        ]
    matrix_by_id = {item.criterion_id: item for item in matrix.items}
    packet_by_key = {
        (packet.criterion_id, packet.evidence_id): packet
        for packet in evidence_packets.packets
    }
    entries: list[EvidenceMapEntry] = []
    for entry in evidence_map.entries:
        if not entry.required:
            continue
        item = matrix_by_id.get(entry.criterion_id)
        packet = packet_by_key.get((entry.criterion_id, entry.evidence_id))
        packet_needs_ocr = bool(packet and any("OCR required" in warning for warning in packet.warnings))
        if (
            entry.status in {"missing", "unclear"}
            or packet_needs_ocr
            or evidence_entry_has_unreadable_document_signal(vendor_docs, entry, item)
        ):
            entries.append(entry)
    if not entries:
        return []
    candidates = targeted_ocr_candidates(vendor_docs, entries, matrix_by_id, logical_documents)
    return run_ocr_candidates(
        candidates,
        EVIDENCE_RELEVANT_OCR_MAX_PAGES,
        f"{BASE_RFP_OCR_PROVIDER}_evidence_relevant_ocr",
    )


def ocr_pdf_page_with_configured_provider(path: Path, zero_based_page: int, page_id: str) -> str:
    if BASE_RFP_OCR_PROVIDER == "paddle":
        layout = ocr_pdf_page_layout_with_paddle(path, zero_based_page, page_id)
        return clean_text(str(layout.get("text") or ""))
    if BASE_RFP_OCR_PROVIDER in {"paddle_api", "openparser"}:
        provider = (
            ocr_pdf_pages_layout_with_openparser
            if BASE_RFP_OCR_PROVIDER == "openparser"
            else ocr_pdf_pages_layout_with_paddle_api
        )
        layouts = provider(path, [zero_based_page], [page_id])
        return clean_text(str((layouts[0] if layouts else {}).get("text") or ""))
    if BASE_RFP_OCR_PROVIDER in {"gemini", "ai"}:
        return clean_text(ocr_pdf_page_with_gemini(path, zero_based_page))
    raise ValueError(f"Unsupported BASE_RFP_OCR_PROVIDER: {BASE_RFP_OCR_PROVIDER}")


def ocr_result_cache_provider_key() -> str:
    profile = hashlib.sha256(
        json.dumps(
            {
                "provider": BASE_RFP_OCR_PROVIDER,
                "model": OPENPARSER_OCR_MODEL if BASE_RFP_OCR_PROVIDER == "openparser" else BASE_RFP_PADDLE_API_MODEL,
                "seal": os.getenv("PADDLEOCR_USE_SEAL_RECOGNITION", "1"),
                "image_blocks": os.getenv("PADDLEOCR_USE_OCR_FOR_IMAGE_BLOCK", "1"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return re.sub(r"[^a-z0-9_.-]+", "-", BASE_RFP_OCR_PROVIDER.lower()) + "-" + profile


def run_ocr_candidates(
    candidates: list[tuple[int, ExtractedDocument, PageUnit, str, str]],
    max_pages: int,
    extraction_method: str,
) -> list[dict[str, Any]]:
    selected = select_ocr_candidates(candidates, max_pages)
    if not selected:
        return []
    ocr_started = time.perf_counter()
    logger.info(
        "OCR started job_id=%s provider=%s phase=%s pages=%s",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        BASE_RFP_OCR_PROVIDER,
        extraction_method,
        len(selected),
    )

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)
    worker_provider_operation_id = current_operation_id()
    document_hashes: dict[str, str] = {}
    for _score, document, _page, _reason, _evidence_key in selected:
        if document.record.document_id in document_hashes or not document.source_path:
            continue
        try:
            with document.source_path.open("rb") as handle:
                document_hashes[document.record.document_id] = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError:
            document_hashes[document.record.document_id] = document.record.document_id
    provider_key = ocr_result_cache_provider_key()

    def cache_path(item: tuple[int, ExtractedDocument, PageUnit, str, str]) -> Path | None:
        _score, document, page, _reason, _evidence_key = item
        document_hash = document_hashes.get(document.record.document_id)
        if not document_hash:
            return None
        return OCR_RESULT_CACHE_DIR / provider_key / document_hash / f"page-{page.page_number}.json"

    def cached_text(item: tuple[int, ExtractedDocument, PageUnit, str, str]) -> str:
        stored = cache_path(item)
        if not stored or not stored.is_file():
            return ""
        try:
            payload = json.loads(stored.read_text(encoding="utf-8"))
            text, metrics = validate_ocr_text(payload.get("text") or "")
            quality = min(
                float(payload.get("quality_score") or metrics["score"]),
                float(metrics["score"]),
            )
            return text if quality >= BASE_RFP_OCR_MIN_CONFIDENCE else ""
        except (OSError, ValueError, json.JSONDecodeError):
            return ""

    def ocr_one(item: tuple[int, ExtractedDocument, PageUnit, str, str]) -> tuple[tuple[int, ExtractedDocument, PageUnit, str, str], str, Exception | None, bool]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        JOB_CONTEXT.provider_operation_id = worker_provider_operation_id
        _score, document, page, _reason, _evidence_key = item
        if not document.source_path:
            return item, "", None, False
        text = cached_text(item)
        if text:
            return item, text, None, True
        stored = cache_path(item)
        call_started = time.monotonic()
        try:
            text, metrics = validate_ocr_text(ocr_pdf_page_with_configured_provider(
                document.source_path,
                page.page_number - 1,
                page.page_id,
            ))
            if stored and text and metrics["score"] >= BASE_RFP_OCR_MIN_CONFIDENCE:
                write_json_atomic(stored, {"text": text, "quality_score": metrics["score"]})
            if BASE_RFP_OCR_PROVIDER == "paddle":
                record_provider_call(
                    capability="ocr",
                    provider="local",
                    model="PPStructureV3",
                    task="local_paddleocr_targeted_page",
                    status="succeeded",
                    duration_ms=round((time.monotonic() - call_started) * 1000),
                    operation_id=worker_provider_operation_id,
                    input_units=1,
                    output_units=1,
                    unit_name="page",
                    request_fingerprint=fingerprint(
                        {"document_id": document.record.document_id, "page": page.page_number}
                    ),
                )
            return item, text, None, False
        except Exception as exc:
            if BASE_RFP_OCR_PROVIDER == "paddle":
                record_provider_call(
                    capability="ocr",
                    provider="local",
                    model="PPStructureV3",
                    task="local_paddleocr_targeted_page",
                    status="failed",
                    duration_ms=round((time.monotonic() - call_started) * 1000),
                    operation_id=worker_provider_operation_id,
                    input_units=1,
                    unit_name="page",
                    request_fingerprint=fingerprint(
                        {"document_id": document.record.document_id, "page": page.page_number}
                    ),
                    error_category=type(exc).__name__.lower()[:64],
                )
            return item, "", exc, False

    if BASE_RFP_OCR_PROVIDER in {"paddle_api", "openparser"}:
        results = []
        pending_by_document: dict[str, list[tuple[int, ExtractedDocument, PageUnit, str, str]]] = {}
        for item in selected:
            text = cached_text(item)
            if text:
                results.append((item, text, None, True))
            else:
                pending_by_document.setdefault(item[1].record.document_id, []).append(item)
        batches = [
            items[index:index + BASE_RFP_PADDLE_API_BATCH_PAGES]
            for items in pending_by_document.values()
            for index in range(0, len(items), BASE_RFP_PADDLE_API_BATCH_PAGES)
        ]

        def ocr_batch(batch: list[tuple[int, ExtractedDocument, PageUnit, str, str]]) -> list[tuple[tuple[int, ExtractedDocument, PageUnit, str, str], str, Exception | None, bool]]:
            JOB_CONTEXT.deadline = worker_deadline
            JOB_CONTEXT.job_id = worker_job_id
            JOB_CONTEXT.llm_usage = worker_usage_log
            JOB_CONTEXT.provider_operation_id = worker_provider_operation_id
            document = batch[0][1]
            last_error: Exception | None = None
            attempts = 1 if len(batch) > 1 else BASE_RFP_PADDLE_API_RETRY_ATTEMPTS
            batch_started = time.monotonic()
            for attempt in range(attempts):
                try:
                    provider = (
                        ocr_pdf_pages_layout_with_openparser
                        if BASE_RFP_OCR_PROVIDER == "openparser"
                        else ocr_pdf_pages_layout_with_paddle_api
                    )
                    layouts = provider(
                        document.source_path,
                        [item[2].page_number - 1 for item in batch],
                        [item[2].page_id for item in batch],
                        usage_attempts=attempt + 1,
                    )
                    batch_results = []
                    for item, layout in zip(batch, layouts):
                        try:
                            normalized = normalize_page_layout_data(layout, item[2].page_id)
                            text = normalized["text"]
                            quality = float(normalized["quality_score"])
                            stored = cache_path(item)
                            if stored and text and quality >= BASE_RFP_OCR_MIN_CONFIDENCE:
                                write_json_atomic(stored, {"text": text, "quality_score": quality})
                            batch_results.append((item, text, None, False))
                        except (TypeError, ValueError) as exc:
                            batch_results.append((item, "", exc, False))
                    return batch_results
                except Exception as exc:
                    last_error = exc
                    check_job_deadline()
                    if attempt + 1 == attempts:
                        failure = classify_provider_exception(
                            exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
                        )
                        provider_name = (
                            "openparser"
                            if BASE_RFP_OCR_PROVIDER == "openparser"
                            else "paddleocr"
                        )
                        model_name = (
                            OPENPARSER_OCR_MODEL
                            if BASE_RFP_OCR_PROVIDER == "openparser"
                            else BASE_RFP_PADDLE_API_MODEL
                        )
                        record_provider_call(
                            capability="ocr",
                            provider=provider_name,
                            model=model_name,
                            task=f"{provider_name}_targeted_document",
                            status="failed",
                            duration_ms=round((time.monotonic() - batch_started) * 1000),
                            operation_id=worker_provider_operation_id,
                            endpoint=(
                                OPENPARSER_BASE_URL
                                if BASE_RFP_OCR_PROVIDER == "openparser"
                                else os.getenv("PADDLEOCR_BASE_URL") or None
                            ),
                            attempts=attempt + 1,
                            input_units=len(batch),
                            unit_name="page",
                            request_fingerprint=fingerprint(
                                {
                                    "document_id": document.record.document_id,
                                    "pages": [item[2].page_number for item in batch],
                                    "model": model_name,
                                }
                            ),
                            error_category=failure.category,
                            http_status=failure.status_code,
                        )
                    if attempt + 1 < attempts:
                        time.sleep(min(2 ** attempt, 4))
            if len(batch) > 1:
                midpoint = len(batch) // 2
                return ocr_batch(batch[:midpoint]) + ocr_batch(batch[midpoint:])
            return [(batch[0], "", last_error or RuntimeError("Hosted OCR failed"), False)]

        if len(batches) <= 1 or BASE_RFP_PADDLE_API_CONCURRENCY == 1:
            for batch in batches:
                results.extend(ocr_batch(batch))
        else:
            with ThreadPoolExecutor(max_workers=min(BASE_RFP_PADDLE_API_CONCURRENCY, len(batches))) as executor:
                for future in as_completed([executor.submit(ocr_batch, batch) for batch in batches]):
                    results.extend(future.result())
    elif BASE_RFP_OCR_PROVIDER == "paddle" or len(selected) == 1:
        results = [ocr_one(item) for item in selected]
    else:
        with ThreadPoolExecutor(max_workers=min(OCR_CONCURRENCY, len(selected))) as executor:
            results = [future.result() for future in [executor.submit(ocr_one, item) for item in selected]]

    cache_hits = sum(bool(result[3]) for result in results)
    if cache_hits:
        settings_value = ai_settings()
        provider_name = (
            "openparser"
            if BASE_RFP_OCR_PROVIDER == "openparser"
            else "paddleocr"
            if BASE_RFP_OCR_PROVIDER == "paddle_api"
            else "local"
            if BASE_RFP_OCR_PROVIDER == "paddle"
            else settings_value.provider
        )
        model_name = (
            OPENPARSER_OCR_MODEL
            if BASE_RFP_OCR_PROVIDER == "openparser"
            else BASE_RFP_PADDLE_API_MODEL
            if BASE_RFP_OCR_PROVIDER == "paddle_api"
            else "PPStructureV3"
            if BASE_RFP_OCR_PROVIDER == "paddle"
            else settings_value.model_ocr
        )
        record_provider_call(
            capability="ocr",
            provider=provider_name,
            model=model_name,
            task="targeted_ocr_cache",
            status="cache_hit",
            duration_ms=0,
            operation_id=worker_provider_operation_id,
            cache_status="hit",
            input_units=cache_hits,
            output_units=cache_hits,
            unit_name="page",
            request_fingerprint=fingerprint(
                [result[0][2].page_id for result in results if result[3]]
            ),
        )

    notes: list[dict[str, Any]] = []
    for item, ocr_text, error, cache_hit in results:
        score, document, page, reason, _evidence_key = item
        if error:
            exc = error
            page.warnings.append(f"targeted OCR failed: {exc}")
            notes.append(
                {
                    "page_id": page.page_id,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "status": "error",
                    "reason": reason,
                    "score": score,
                    "error": str(exc),
                }
            )
            continue
        if not ocr_text:
            page.warnings.append("targeted OCR returned no text")
            notes.append(
                {
                    "page_id": page.page_id,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "status": "empty",
                    "reason": reason,
                    "score": score,
                }
            )
            continue
        page.text = ocr_text[:MAX_DOC_CHARS]
        page.text_chars = len(ocr_text)
        page.extraction_method = extraction_method
        page.extraction_quality = float(page_text_quality(ocr_text)["score"])
        page.quality_metrics = page_text_quality(ocr_text)
        page.ocr_required = page.extraction_quality < BASE_RFP_OCR_MIN_CONFIDENCE
        page.warnings.append(f"targeted OCR completed: {reason}")
        page.classification = heuristic_page_classification(page) or page.classification
        refresh_document_text(document)
        notes.append(
            {
                "page_id": page.page_id,
                "file_name": page.file_name,
                "page_number": page.page_number,
                "status": "low_confidence" if page.ocr_required else "ok",
                "reason": reason,
                "score": score,
                "text_chars": len(ocr_text),
                "quality_score": page.extraction_quality,
                "cache_hit": cache_hit,
            }
        )
    logger.info(
        "OCR complete job_id=%s provider=%s phase=%s pages=%s cache_hits=%s errors=%s duration_seconds=%.3f",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        BASE_RFP_OCR_PROVIDER,
        extraction_method,
        len(notes),
        sum(bool(note.get("cache_hit")) for note in notes),
        sum(note.get("status") == "error" for note in notes),
        time.perf_counter() - ocr_started,
    )
    return notes


def select_ocr_candidates(
    candidates: list[tuple[int, ExtractedDocument, PageUnit, str, str]],
    max_pages: int,
) -> list[tuple[int, ExtractedDocument, PageUnit, str, str]]:
    selected: list[tuple[int, ExtractedDocument, PageUnit, str, str]] = []
    seen: set[str] = set()

    def add(candidate: tuple[int, ExtractedDocument, PageUnit, str, str]) -> None:
        if len(selected) >= max_pages:
            return
        if candidate[2].page_id in seen:
            return
        seen.add(candidate[2].page_id)
        selected.append(candidate)

    by_evidence: dict[str, list[tuple[int, ExtractedDocument, PageUnit, str, str]]] = {}
    for candidate in candidates:
        by_evidence.setdefault(candidate[4], []).append(candidate)
    for evidence_candidates in sorted(by_evidence.values(), key=lambda group: group[0][0], reverse=True):
        for candidate in evidence_candidates[:3]:
            add(candidate)
    for candidate in candidates:
        add(candidate)
    return selected


def targeted_ocr_candidates(
    vendor_docs: list[ExtractedDocument],
    entries: list[EvidenceMapEntry],
    matrix_by_id: dict[str, RequirementMatrixItem],
    logical_documents: list[LogicalDocument],
) -> list[tuple[int, ExtractedDocument, PageUnit, str]]:
    logical_by_id = {doc.logical_document_id: doc for doc in logical_documents}
    page_by_id = {
        page.page_id: page
        for document in vendor_docs
        for page in document.pages
    }
    candidates: list[tuple[int, ExtractedDocument, PageUnit, str, str]] = []
    for entry in entries:
        evidence_key = f"{entry.criterion_id}:{entry.evidence_id}"
        item = matrix_by_id.get(entry.criterion_id)
        query = " ".join(
            [
                entry.document_name,
                entry.document_type,
                " ".join(entry.checks),
                item.requirement if item else "",
                item.pass_rule if item else "",
            ]
        )
        query_tokens = keyword_tokens(query)
        evidence = EvidenceRequirement(
            evidence_id=entry.evidence_id,
            document_name=entry.document_name,
            document_type=entry.document_type,
            checks=entry.checks,
        )
        needs_document_sweep = evidence_needs_document_sweep(entry)
        matched_pages = set(entry.matched_page_ids)
        matched_page_units = [page_by_id[page_id] for page_id in matched_pages if page_id in page_by_id]
        matched_document_ids = {page.document_id for page in matched_page_units}
        matched_logical_pages = {
            page_id
            for logical_id in entry.matched_logical_document_ids
            for page_id in logical_by_id.get(logical_id, LogicalDocument(logical_document_id=logical_id, upload_side="vendor", title="")).page_refs
        }
        matched_logical_document_ids = {
            page_by_id[page_id].document_id
            for page_id in matched_logical_pages
            if page_id in page_by_id
        }
        for document in vendor_docs:
            if not document.source_path or document.source_path.suffix.lower() != ".pdf":
                continue
            doc_context = document_context_for_scoring(document)
            doc_score = len(query_tokens & keyword_tokens(doc_context)) + evidence_marker_score(evidence, doc_context)
            document_signal = document_required_evidence_signal(document, evidence, item)
            weak_pages = [page for page in document.pages if page.ocr_required or not page.text.strip()]
            for page in weak_pages:
                context = surrounding_page_text(document, page.page_number)
                score = doc_score + len(query_tokens & keyword_tokens(context)) + evidence_marker_score(evidence, context)
                has_document_signal = (
                    doc_score > 0
                    or document_signal
                    or procurement_filename_signal(document.record.file_name)
                    or document.record.document_id in matched_document_ids
                    or document.record.document_id in matched_logical_document_ids
                )
                reasons = []
                if document_signal and needs_document_sweep:
                    score += 35
                    reasons.append(f"unreadable page inside matching evidence document for {entry.document_name}")
                elif document_signal and page.page_number <= EVIDENCE_OCR_DOCUMENT_SIGNAL_PAGES:
                    score += 40
                    reasons.append(f"unreadable document identity matches {entry.document_name}")
                if page.page_id in matched_pages:
                    score += 12
                    reasons.append(f"matched evidence page for {entry.document_name}")
                if any(page.document_id == matched.document_id and 0 < abs(page.page_number - matched.page_number) <= max(2, EVIDENCE_PACKET_NEIGHBOR_PAGES) for matched in matched_page_units):
                    score += 10
                    reasons.append(f"near matched evidence page for {entry.document_name}")
                if page.page_id in matched_logical_pages:
                    score += 8
                    reasons.append(f"inside matched logical document for {entry.document_name}")
                if not page.text.strip() and page.page_number == 1 and has_document_signal:
                    score += 2 if not doc_context.strip() else 1
                    reasons.append(f"first unreadable page while checking {entry.document_name}")
                if score >= 2 or reasons:
                    candidates.append((score, document, page, "; ".join(reasons) or f"candidate page for {entry.document_name}", evidence_key))
    candidates.sort(key=lambda item: (item[0], item[2].text_chars == 0), reverse=True)
    return candidates


def evidence_needs_document_sweep(entry: EvidenceMapEntry) -> bool:
    checks = {normalize_check_key(check) for check in entry.checks}
    doc_type = canonical_evidence_type(entry.document_type)
    return doc_type in {"turnover_networth_ca_certificate", "audited_financials_or_itr"} and bool(
        checks & {"financial_years", "value_threshold", "udin", "udin_if_ca"}
    )


def evidence_entry_has_unreadable_document_signal(
    vendor_docs: list[ExtractedDocument],
    entry: EvidenceMapEntry,
    item: RequirementMatrixItem | None,
) -> bool:
    evidence = EvidenceRequirement(
        evidence_id=entry.evidence_id,
        document_name=entry.document_name,
        document_type=entry.document_type,
        checks=entry.checks,
    )
    return any(
        document.source_path
        and document.source_path.suffix.lower() == ".pdf"
        and any(page.ocr_required or not page.text.strip() for page in document.pages)
        and document_required_evidence_signal(document, evidence, item)
        for document in vendor_docs
    )


def document_required_evidence_signal(
    document: ExtractedDocument,
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem | None,
) -> bool:
    classification = document.record.classification
    document_text = normalize_for_match(
        " ".join(
            [
                document.record.file_name,
                classification.document_type if classification else "",
                classification.content_role if classification else "",
            ]
        )
    )
    requirement_text = normalize_for_match(
        " ".join(
            [
                evidence.document_name,
                evidence.document_type,
                " ".join(evidence.checks),
                item.requirement if item else "",
                item.pass_rule if item else "",
            ]
        )
    )
    doc_type = canonical_evidence_type(evidence.document_type)
    family_markers = {
        "turnover_networth_ca_certificate": ["financial", "audited", "balance sheet", "profit loss", "turnover", "net worth", "ca certificate"],
        "experience_po_completion": ["purchase order", "work order", "completion", "performance", "experience", "technical work"],
        "integrity_blacklisting_declaration": ["integrity", "pact", "blacklist", "holiday", "debar"],
        "mii_local_content": ["local content", "make in india", "mii", "annexure"],
        "gst_pan_cin_statutory": ["pan", "gst", "gstin", "cin"],
        "mse_udyam_or_startup_exemption": ["udyam", "msme", "mse"],
        "emd_or_bid_security": ["emd", "bid security", "bank guarantee"],
        "calibration_certificate": [
            "calibration certificate", "calibration record", "calibrated on", "instrument id",
        ],
    }
    if any(marker in document_text for marker in family_markers.get(doc_type, [])):
        return True
    document_tokens = keyword_tokens(document_text)
    requirement_tokens = {
        token
        for token in keyword_tokens(requirement_text)
        if token not in {"copy", "proof", "evidence", "required", "support", "complete"}
    }
    return len(document_tokens & requirement_tokens) >= 2


def document_context_for_scoring(document: ExtractedDocument) -> str:
    classification = document.record.classification
    return " ".join(
        [
            document.record.file_name,
            classification.document_type if classification else "",
            document.text[:4000],
        ]
    )


def procurement_filename_signal(file_name: str) -> bool:
    name = normalize_for_match(file_name)
    return any(term in name for term in ["financial", "technical", "annexure", "declaration", "integrity", "pact", "gst", "pan"])


def surrounding_page_text(document: ExtractedDocument, page_number: int) -> str:
    parts = []
    for page in document.pages:
        if abs(page.page_number - page_number) <= 1:
            parts.append(page.text)
    return clean_text("\n".join(parts))


def refresh_document_text(document: ExtractedDocument) -> None:
    text = clean_text(
        "\n\n".join(
            f"[page {page.page_number}]\n{page.text}"
            for page in document.pages
            if page.text
        )
    )
    document.text = text[:MAX_DOC_CHARS]
    document.record.text_chars = len(text)
    document.record.extraction_status = "ok" if text else "empty"


def snippets_for_evidence_entry(
    entry: EvidenceMapEntry,
    page_text_by_id: dict[str, str],
    doc_text_by_logical_id: dict[str, str],
) -> list[str]:
    snippets = [page_text_by_id[page_id][:2500] for page_id in entry.matched_page_ids if page_id in page_text_by_id]
    if snippets:
        return snippets
    return [
        doc_text_by_logical_id.get(logical_id, "")[:2500]
        for logical_id in entry.matched_logical_document_ids
        if doc_text_by_logical_id.get(logical_id)
    ]


def snippets_for_evidence_packet(
    entry: EvidenceMapEntry,
    evidence_packets: EvidencePacketSet | None,
    *,
    max_pages: int = EVIDENCE_PACKET_MAX_PAGES,
) -> list[str]:
    if not evidence_packets:
        return []
    snippets = []
    for packet in evidence_packets.packets:
        if packet.criterion_id != entry.criterion_id or packet.evidence_id != entry.evidence_id:
            continue
        snippets.extend(
            part[:EVIDENCE_VERIFIER_PAGE_CHARS]
            for part in re.split(r"(?=^\[page \d+\]\n)", packet.text, flags=re.MULTILINE)
            if part.strip()
        )
    return snippets[:max_pages]
