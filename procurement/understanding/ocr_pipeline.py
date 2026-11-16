"""OCR orchestration for scanned tender and bidder pages."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ai_provider import settings as ai_settings
from procurement.domain.schemas import ExtractedDocument, PageUnit
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text
from procurement.platform.job_context import JOB_CONTEXT, check_job_deadline
from procurement.platform.files import write_json_atomic
from procurement.platform.provider_usage import (
    current_operation_id,
    fingerprint,
    record_provider_call,
)
from procurement.providers.errors import classify_provider_exception
from procurement.platform.llm import (
    GEMINI_MODEL_FAST,
    GEMINI_THINKING_BUDGET,
    PROCUREMENT_OFFLINE_LLM,
    GeminiOutputTruncated,
    gemini_api_key,
    parse_json_object,
    post_gemini_content,
)
from procurement.platform.prompts import prompt_text
from procurement.understanding.layout import (
    layout_tables_from_text,
    normalize_page_layout_data,
)
from procurement.understanding.ocr_providers import (
    BASE_RFP_PADDLE_API_MODEL,
    BASE_RFP_PADDLE_DEVICE,
    BASE_RFP_OCR_MIN_CONFIDENCE,
    OPENPARSER_BASE_URL,
    OPENPARSER_OCR_MODEL,
    ocr_pdf_page_layout_with_paddle,
    ocr_pdf_pages_layout_with_openparser,
    ocr_pdf_pages_layout_with_paddle_api,
)
from procurement.understanding.quality import page_text_quality
from procurement.understanding.rendering import pdf_page_base64, render_pdf_page_png_b64


logger = logging.getLogger("procurement_evaluator")
BASE_RFP_OCR_ENABLED = os.getenv("BASE_RFP_OCR_ENABLED", "1") != "0"
BASE_RFP_OCR_MAX_PAGES = int(os.getenv("BASE_RFP_OCR_MAX_PAGES", "0"))
BASE_RFP_OCR_PROVIDER = os.getenv("BASE_RFP_OCR_PROVIDER", "paddle").strip().lower()
BASE_RFP_PADDLE_API_BATCH_PAGES = max(
    1, min(100, int(os.getenv("BASE_RFP_PADDLE_API_BATCH_PAGES", "8")))
)
BASE_RFP_PADDLE_API_CONCURRENCY = max(
    1, min(8, int(os.getenv("BASE_RFP_PADDLE_API_CONCURRENCY", "2")))
)
BASE_RFP_PADDLE_API_RETRY_ATTEMPTS = max(
    1, min(5, int(os.getenv("BASE_RFP_PADDLE_API_RETRY_ATTEMPTS", "3")))
)
BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED = os.getenv(
    "BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED", "0"
) == "1"
OCR_CONCURRENCY = max(1, int(os.getenv("OCR_CONCURRENCY", "4")))
OCR_RETRY_ATTEMPTS = max(1, min(4, int(os.getenv("OCR_RETRY_ATTEMPTS", "2"))))
OCR_TILE_MAX_DEPTH = max(2, min(3, int(os.getenv("OCR_TILE_MAX_DEPTH", "3"))))
OCR_TILE_OVERLAP_PERCENT = max(
    0, min(10, int(os.getenv("OCR_TILE_OVERLAP_PERCENT", "2")))
)
AI_OCR_PROMPT = os.getenv(
    "AI_OCR_PROMPT",
    "OCR:\nReturn plain text only. Do not emit Unicode control characters. "
    "Replace any unreadable glyph with a question mark.",
)
OCR_RESULT_CACHE_DIR = Path(os.getenv("OCR_RESULT_CACHE_DIR", "runtime/cache/ocr-results"))


def split_png_horizontally(image_b64: str) -> list[str]:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(image_b64))) as image:
        width, height = image.size
        midpoint = height // 2
        overlap = max(1, int(height * OCR_TILE_OVERLAP_PERCENT / 100))
        boxes = [
            (0, 0, width, min(height, midpoint + overlap)),
            (0, max(0, midpoint - overlap), width, height),
        ]
        encoded: list[str] = []
        for box in boxes:
            with image.crop(box) as tile:
                buffer = io.BytesIO()
                tile.save(buffer, format="PNG")
                encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
        return encoded


def merge_ocr_segments(values: list[str]) -> str:
    merged: list[str] = []
    for value in values:
        lines = [line.rstrip() for line in str(value or "").splitlines()]
        overlap = next(
            (
                count
                for count in range(min(20, len(merged), len(lines)), 0, -1)
                if merged[-count:] == lines[:count]
            ),
            0,
        )
        merged.extend(lines[overlap:])
    return clean_text("\n".join(merged))


def ocr_openai_compatible_page_tiles(path: Path, zero_based_page: int) -> str:
    settings_value = ai_settings()

    def extract(image_b64: str, depth: int) -> list[str]:
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "OCR this horizontal page segment. Return every visible character once, "
                                "top-to-bottom. Preserve tables compactly. Do not describe or summarize."
                            )
                        },
                        {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": int(os.getenv("AI_OCR_MAX_OUTPUT_TOKENS", "4096")),
            },
        }
        try:
            return [
                post_gemini_content(
                    "", settings_value.model_ocr, body, task="ocr_pdf_page_tile"
                )
            ]
        except GeminiOutputTruncated:
            if depth >= OCR_TILE_MAX_DEPTH:
                raise
            return [
                text
                for tile in split_png_horizontally(image_b64)
                for text in extract(tile, depth + 1)
            ]

    page_image = render_pdf_page_png_b64(path, zero_based_page)
    quarters = [
        quarter
        for half in split_png_horizontally(page_image)
        for quarter in split_png_horizontally(half)
    ]
    return merge_ocr_segments(
        [text for quarter in quarters for text in extract(quarter, 2)]
    )


def ocr_pdf_page_with_gemini(path: Path, zero_based_page: int) -> str:
    return str(ocr_pdf_page_layout_with_gemini(path, zero_based_page).get("text") or "")


def ocr_pdf_page_layout_with_gemini(
    path: Path,
    zero_based_page: int,
    page_id: str | None = None,
) -> dict[str, Any]:
    provider_settings = ai_settings()
    ocr_provider = os.getenv("AI_OCR_PROVIDER", provider_settings.provider).strip().lower()
    if ocr_provider in {"openai", "openai-compatible", "openai_compatible", "yotta", "yota"}:
        page_b64 = pdf_page_base64(path, zero_based_page)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": "application/pdf", "data": page_b64}},
                        {"text": AI_OCR_PROMPT},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": int(os.getenv("AI_OCR_MAX_OUTPUT_TOKENS", "4096")),
            },
        }
        try:
            raw = post_gemini_content(
                "",
                provider_settings.model_ocr,
                body,
                task="ocr_pdf_page",
            )
            provider = "openai_compatible_ocr"
        except GeminiOutputTruncated:
            raw = ocr_openai_compatible_page_tiles(path, zero_based_page)
            provider = "openai_compatible_ocr_tiled"
        return normalize_page_layout_data(
            {
                "text": raw,
                "provider": provider,
                "tables": [
                    model_dump(table)
                    for table in layout_tables_from_text(
                        raw,
                        page_id or f"ocr-p{zero_based_page + 1}",
                    )
                ],
            },
            page_id or f"ocr-p{zero_based_page + 1}",
        )

    if not gemini_api_key():
        raise RuntimeError("The configured AI provider is unavailable for OCR")
    page_b64 = pdf_page_base64(path, zero_based_page)
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt_text(
                            "ocr_pdf_page",
                            "Extract this single-page Indian procurement PDF without flattening its layout. "
                            "Return every readable block in natural reading order and every table as explicit cells. "
                            "Do not summarize, omit, normalize, or infer text. Return JSON only.",
                        )
                    },
                    {"inline_data": {"mime_type": "application/pdf", "data": page_b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
        },
    }
    body["contents"][0]["parts"][0]["text"] += "\nJSON shape: " + json.dumps(
        {
            "text": "all readable text in natural reading order",
            "blocks": [
                {
                    "block_type": "heading|paragraph|list_item|header|footer|caption|other",
                    "text": "verbatim block text",
                    "reading_order": 0,
                    "bbox": [0.0, 0.0, 1.0, 1.0],
                }
            ],
            "tables": [
                {
                    "title": "table title if visible",
                    "markdown": "lossless markdown rendering",
                    "bbox": [0.0, 0.0, 1.0, 1.0],
                    "cells": [
                        {
                            "row_index": 0,
                            "column_index": 0,
                            "row_span": 1,
                            "column_span": 1,
                            "is_header": False,
                            "text": "verbatim cell text",
                            "bbox": [0.0, 0.0, 1.0, 1.0],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=True,
    )
    data = parse_json_object(post_gemini_content(gemini_api_key(), GEMINI_MODEL_FAST, body))
    normalized = normalize_page_layout_data(
        data, page_id or f"ocr-p{zero_based_page + 1}"
    )
    normalized["provider"] = "gemini_visual_fallback"
    return normalized


def run_base_rfp_ocr(
    documents: list[ExtractedDocument],
    checkpoint_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if not BASE_RFP_OCR_ENABLED or PROCUREMENT_OFFLINE_LLM:
        return []
    if BASE_RFP_OCR_PROVIDER not in {"paddle", "paddle_api", "openparser", "gemini", "ai"}:
        raise ValueError(f"Unsupported BASE_RFP_OCR_PROVIDER: {BASE_RFP_OCR_PROVIDER}")
    candidates = [
        (document, page)
        for document in documents
        if (
            document.source_path
            and document.source_path.suffix.lower() == ".pdf"
        )
        for page in document.pages
        if page.ocr_required
    ]
    if BASE_RFP_OCR_MAX_PAGES > 0:
        candidates = candidates[:BASE_RFP_OCR_MAX_PAGES]
    if not candidates:
        logger.info(
            "OCR skipped job_id=%s provider=%s reason=no_scanned_pages",
            getattr(JOB_CONTEXT, "job_id", "unknown"),
            BASE_RFP_OCR_PROVIDER,
        )
        return []
    ocr_started = time.perf_counter()
    logger.info(
        "OCR started job_id=%s provider=%s pages=%s",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        BASE_RFP_OCR_PROVIDER,
        len(candidates),
    )

    checkpoint_paths: dict[tuple[str, str], Path] = {}
    if checkpoint_dir:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        settings_value = ai_settings()
        cache_identity = hashlib.sha256(
            json.dumps(
                {
                    "version": "ocr-cache-v2",
                    "revision": os.getenv(
                        "AI_CACHE_REVISION",
                        os.getenv("AI_DEPLOYMENT_REVISION", "1"),
                    ),
                    "provider": BASE_RFP_OCR_PROVIDER,
                    "ai_provider": settings_value.provider,
                    "ai_base_url": settings_value.ocr_base_url.rstrip("/"),
                    "ai_model": settings_value.model_ocr,
                    "openparser_base_url": OPENPARSER_BASE_URL,
                    "openparser_model": OPENPARSER_OCR_MODEL,
                    "paddle_api_base_url": os.getenv("PADDLEOCR_BASE_URL", "").rstrip("/"),
                    "paddle_api_model": BASE_RFP_PADDLE_API_MODEL,
                    "paddle_device": BASE_RFP_PADDLE_DEVICE,
                    "seal_recognition": os.getenv("PADDLEOCR_USE_SEAL_RECOGNITION", "1"),
                    "image_block_ocr": os.getenv("PADDLEOCR_USE_OCR_FOR_IMAGE_BLOCK", "1"),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        hashes: dict[str, str] = {}
        for document, _page in candidates:
            if document.record.document_id in hashes:
                continue
            try:
                with document.source_path.open("rb") as handle:
                    hashes[document.record.document_id] = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError:
                hashes[document.record.document_id] = document.record.document_id
        checkpoint_paths = {
            (document.record.document_id, page.page_id): checkpoint_dir
            / BASE_RFP_OCR_PROVIDER
            / cache_identity
            / hashes[document.record.document_id]
            / f"page-{page.page_number}.json"
            for document, page in candidates
        }

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)
    worker_provider_operation_id = current_operation_id()

    def ocr_one(item: tuple[ExtractedDocument, PageUnit]) -> tuple[ExtractedDocument, PageUnit, dict[str, Any] | None, Exception | None]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        JOB_CONTEXT.provider_operation_id = worker_provider_operation_id
        document, page = item
        last_error: Exception | None = None
        for attempt in range(OCR_RETRY_ATTEMPTS):
            call_started: float | None = None
            try:
                if BASE_RFP_OCR_PROVIDER == "paddle":
                    call_started = time.monotonic()
                    layout = ocr_pdf_page_layout_with_paddle(document.source_path, page.page_number - 1, page.page_id)
                    record_provider_call(
                        capability="ocr",
                        provider="local",
                        model="PPStructureV3",
                        task="local_paddleocr_page",
                        status="succeeded",
                        duration_ms=round((time.monotonic() - call_started) * 1000),
                        operation_id=worker_provider_operation_id,
                        attempts=attempt + 1,
                        input_units=1,
                        output_units=1,
                        unit_name="page",
                        request_fingerprint=fingerprint(
                            {"document_id": document.record.document_id, "page": page.page_number}
                        ),
                    )
                    if BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED:
                        raise RuntimeError("BASE_RFP_VISUAL_OCR_FALLBACK_ENABLED is no longer supported; select one OCR provider")
                elif BASE_RFP_OCR_PROVIDER in {"gemini", "ai"}:
                    layout = ocr_pdf_page_layout_with_gemini(document.source_path, page.page_number - 1, page.page_id)
                else:
                    raise RuntimeError("hosted OCR must run through the batched document path")
                return document, page, layout, None
            except Exception as exc:
                if (
                    BASE_RFP_OCR_PROVIDER == "paddle"
                    and call_started is not None
                    and attempt + 1 == OCR_RETRY_ATTEMPTS
                ):
                    record_provider_call(
                        capability="ocr",
                        provider="local",
                        model="PPStructureV3",
                        task="local_paddleocr_page",
                        status="failed",
                        duration_ms=round((time.monotonic() - call_started) * 1000),
                        operation_id=worker_provider_operation_id,
                        attempts=attempt + 1,
                        input_units=1,
                        unit_name="page",
                        request_fingerprint=fingerprint(
                            {"document_id": document.record.document_id, "page": page.page_number}
                        ),
                        error_category=type(exc).__name__.lower()[:64],
                    )
                last_error = exc
                check_job_deadline()
                if attempt + 1 < OCR_RETRY_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 4))
        return document, page, None, last_error

    results: list[tuple[ExtractedDocument, PageUnit, dict[str, Any] | None, Exception | None, bool]] = []
    pending: list[tuple[ExtractedDocument, PageUnit]] = []
    for document, page in candidates:
        cache_path = checkpoint_paths.get((document.record.document_id, page.page_id))
        if cache_path and cache_path.is_file():
            try:
                cached = normalize_page_layout_data(json.loads(cache_path.read_text(encoding="utf-8")), page.page_id)
                if (
                    cached.get("blank_visual_page")
                    or float(cached.get("quality_score") or 0) >= BASE_RFP_OCR_MIN_CONFIDENCE
                ):
                    results.append((document, page, cached, None, True))
                    continue
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        pending.append((document, page))

    cache_hits = len(results)
    if cache_hits:
        settings_value = ai_settings()
        provider_name = (
            "openparser"
            if BASE_RFP_OCR_PROVIDER == "openparser"
            else "paddleocr"
            if BASE_RFP_OCR_PROVIDER in {"paddle", "paddle_api"}
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
            task="ocr_cache",
            status="cache_hit",
            duration_ms=0,
            operation_id=worker_provider_operation_id,
            cache_status="hit",
            input_units=cache_hits,
            output_units=cache_hits,
            unit_name="page",
            request_fingerprint=fingerprint(
                [page.page_id for _document, page, _layout, _error, _hit in results]
            ),
        )

    def keep_result(result: tuple[ExtractedDocument, PageUnit, dict[str, Any] | None, Exception | None]) -> None:
        document, page, layout, error = result
        if layout and not error:
            try:
                layout = normalize_page_layout_data(
                    {
                        **layout,
                        "blocks": [model_dump(item) for item in layout.get("blocks") or []],
                        "tables": [model_dump(item) for item in layout.get("tables") or []],
                    },
                    page.page_id,
                )
            except (TypeError, ValueError) as exc:
                layout, error = None, exc
        if layout and not error and (
            layout.get("blank_visual_page")
            or float(layout.get("quality_score") or 0) >= BASE_RFP_OCR_MIN_CONFIDENCE
        ):
            cache_path = checkpoint_paths.get((document.record.document_id, page.page_id))
            if cache_path:
                write_json_atomic(
                    cache_path,
                    {
                        "text": layout.get("text", ""),
                        "blocks": [model_dump(item) for item in layout.get("blocks") or []],
                        "tables": [model_dump(item) for item in layout.get("tables") or []],
                        "provider": layout.get("provider", ""),
                        "quality_score": layout.get("quality_score", 0),
                        "quality_metrics": layout.get("quality_metrics") or {},
                        "blank_visual_page": bool(layout.get("blank_visual_page")),
                    },
                )
        results.append((document, page, layout, error, False))

    if BASE_RFP_OCR_PROVIDER in {"paddle_api", "openparser"}:
        by_document: dict[str, list[tuple[ExtractedDocument, PageUnit]]] = {}
        for item in pending:
            by_document.setdefault(item[0].record.document_id, []).append(item)
        batches = [
            ordered[start : start + BASE_RFP_PADDLE_API_BATCH_PAGES]
            for items in by_document.values()
            for ordered in [sorted(items, key=lambda item: item[1].page_number)]
            for start in range(0, len(ordered), BASE_RFP_PADDLE_API_BATCH_PAGES)
        ]

        def ocr_api_batch(
            batch: list[tuple[ExtractedDocument, PageUnit]],
        ) -> list[tuple[ExtractedDocument, PageUnit, dict[str, Any] | None, Exception | None]]:
            JOB_CONTEXT.deadline = worker_deadline
            JOB_CONTEXT.job_id = worker_job_id
            JOB_CONTEXT.llm_usage = worker_usage_log
            JOB_CONTEXT.provider_operation_id = worker_provider_operation_id
            document = batch[0][0]
            last_error: Exception | None = None
            attempts = 1 if len(batch) > 1 else BASE_RFP_PADDLE_API_RETRY_ATTEMPTS
            for attempt in range(attempts):
                call_started = time.monotonic()
                try:
                    provider = (
                        ocr_pdf_pages_layout_with_openparser
                        if BASE_RFP_OCR_PROVIDER == "openparser"
                        else ocr_pdf_pages_layout_with_paddle_api
                    )
                    layouts = provider(
                        document.source_path,
                        [page.page_number - 1 for _document, page in batch],
                        [page.page_id for _document, page in batch],
                        usage_attempts=attempt + 1,
                    )
                    return [
                        (document, page, layout, None)
                        for (_document, page), layout in zip(batch, layouts)
                    ]
                except Exception as exc:
                    cause = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
                    failure = classify_provider_exception(cause)
                    provider_name = "openparser" if BASE_RFP_OCR_PROVIDER == "openparser" else "paddleocr"
                    model_name = OPENPARSER_OCR_MODEL if BASE_RFP_OCR_PROVIDER == "openparser" else BASE_RFP_PADDLE_API_MODEL
                    endpoint = OPENPARSER_BASE_URL if BASE_RFP_OCR_PROVIDER == "openparser" else os.getenv("PADDLEOCR_BASE_URL") or None
                    if attempt + 1 == attempts:
                        record_provider_call(
                            capability="ocr",
                            provider=provider_name,
                            model=model_name,
                            task=f"{provider_name}_document",
                            status="failed",
                            duration_ms=round((time.monotonic() - call_started) * 1000),
                            operation_id=worker_provider_operation_id,
                            attempts=attempt + 1,
                            input_units=len(batch),
                            unit_name="page",
                            request_fingerprint=fingerprint(
                                {
                                    "document_id": document.record.document_id,
                                    "pages": [page.page_number for _document, page in batch],
                                    "model": model_name,
                                }
                            ),
                            error_category=failure.category,
                            http_status=failure.status_code,
                        )
                    last_error = exc
                    check_job_deadline()
                    if attempt + 1 == attempts:
                        break
                    time.sleep(min(2 ** attempt, 4))
            if len(batch) > 1:
                midpoint = len(batch) // 2
                return ocr_api_batch(batch[:midpoint]) + ocr_api_batch(batch[midpoint:])
            return [(document, batch[0][1], None, last_error or RuntimeError("Hosted OCR failed"))]

        if len(batches) <= 1 or BASE_RFP_PADDLE_API_CONCURRENCY == 1:
            for batch in batches:
                for result in ocr_api_batch(batch):
                    keep_result(result)
        else:
            with ThreadPoolExecutor(max_workers=min(BASE_RFP_PADDLE_API_CONCURRENCY, len(batches))) as executor:
                futures = [executor.submit(ocr_api_batch, batch) for batch in batches]
                for future in as_completed(futures):
                    for result in future.result():
                        keep_result(result)
    else:
        concurrency = 1 if BASE_RFP_OCR_PROVIDER == "paddle" else OCR_CONCURRENCY
        if concurrency == 1 or len(pending) <= 1:
            for item in pending:
                keep_result(ocr_one(item))
        else:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
                futures = [executor.submit(ocr_one, item) for item in pending]
                for future in as_completed(futures):
                    keep_result(future.result())

    notes: list[dict[str, Any]] = []
    changed_documents: set[str] = set()
    for document, page, layout, error, cache_hit in results:
        if error:
            page.warnings.append(f"layout OCR failed: {error}")
            notes.append({"page_id": page.page_id, "page_number": page.page_number, "status": "error", "error": str(error)[:400]})
            continue
        text = clean_text(str((layout or {}).get("text") or ""))
        if bool((layout or {}).get("blank_visual_page")):
            page.text = ""
            page.layout_text = ""
            page.blocks = []
            page.tables = []
            page.text_chars = 0
            page.extraction_method = str((layout or {}).get("provider") or "visual_blank_page")
            page.extraction_quality = 1.0
            page.quality_metrics = dict((layout or {}).get("quality_metrics") or {"blank_visual_page": True})
            page.is_blank = True
            page.ocr_required = False
            page.warnings.append("intentional blank physical page restored from checkpoint" if cache_hit else "intentional blank physical page detected")
            changed_documents.add(document.record.document_id)
            notes.append(
                {
                    "page_id": page.page_id,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "status": "blank",
                    "provider": page.extraction_method,
                    "quality_score": page.extraction_quality,
                    "cache_hit": cache_hit,
                    "blocks": 0,
                    "tables": 0,
                }
            )
            continue
        if not text:
            page.warnings.append("layout OCR returned no text")
            notes.append({"page_id": page.page_id, "page_number": page.page_number, "status": "empty"})
            continue
        native_quality = min(
            float(page_text_quality(page.text)["score"]) if page.text else 0.0,
            float(page.extraction_quality),
        )
        ocr_quality = float((layout or {}).get("quality_score") or 0.0)
        if page.text and ocr_quality <= native_quality:
            page.warnings.append("layout OCR was not better than native extraction; native text retained")
            notes.append(
                {
                    "page_id": page.page_id,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "status": "low_confidence",
                    "provider": str((layout or {}).get("provider") or BASE_RFP_OCR_PROVIDER),
                    "quality_score": ocr_quality,
                    "cache_hit": cache_hit,
                    "native_text_retained": True,
                }
            )
            continue
        page.text = text
        page.layout_text = text
        page.blocks = list((layout or {}).get("blocks") or [])
        page.tables = list((layout or {}).get("tables") or [])
        page.text_chars = len(text)
        page.extraction_method = str((layout or {}).get("provider") or f"{BASE_RFP_OCR_PROVIDER}_layout_ocr")
        page.extraction_quality = ocr_quality
        page.quality_metrics = dict((layout or {}).get("quality_metrics") or page_text_quality(text))
        page.is_blank = False
        page.ocr_required = page.extraction_quality < BASE_RFP_OCR_MIN_CONFIDENCE
        status = "low_confidence" if page.ocr_required else "ok"
        page.warnings.append("layout OCR restored from checkpoint" if cache_hit else "layout OCR completed")
        if page.ocr_required:
            page.warnings.append("layout OCR confidence is below the configured acceptance threshold")
        changed_documents.add(document.record.document_id)
        notes.append(
            {
                "page_id": page.page_id,
                "file_name": page.file_name,
                "page_number": page.page_number,
                "status": status,
                "provider": page.extraction_method,
                "quality_score": page.extraction_quality,
                "cache_hit": cache_hit,
                "blocks": len(page.blocks),
                "tables": len(page.tables),
            }
        )
    for document in documents:
        if document.record.document_id not in changed_documents:
            continue
        document.text = clean_text("\n\n".join(f"[page {page.page_number}]\n{page.text}" for page in document.pages if page.text))
        document.record.text_chars = len(document.text)
        document.record.extraction_status = "ok" if document.text else "empty"
    logger.info(
        "OCR complete job_id=%s provider=%s pages=%s cache_hits=%s errors=%s duration_seconds=%.3f",
        getattr(JOB_CONTEXT, "job_id", "unknown"),
        BASE_RFP_OCR_PROVIDER,
        len(notes),
        sum(bool(note.get("cache_hit")) for note in notes),
        sum(note.get("status") == "error" for note in notes),
        time.perf_counter() - ocr_started,
    )
    return sorted(notes, key=lambda item: (str(item.get("file_name") or ""), int(item.get("page_number") or 0)))
