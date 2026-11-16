"""OCR provider adapters for local, hosted PaddleOCR, and OpenParser."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from procurement.domain.schemas import LayoutBlock, LayoutTable
from procurement.domain.text import clean_text
from procurement.platform.job_context import job_bounded_timeout_seconds
from procurement.platform.provider_usage import (
    fingerprint,
    record_provider_call,
    check_provider_budget,
)
from procurement.understanding.layout import (
    assign_page_reading_order,
    coerce_bbox,
    coerce_int,
    layout_blocks_from_text,
    layout_tables_from_text,
    normalized_bbox,
    paddle_layout_block_data,
    table_from_html,
)
from procurement.understanding.quality import (
    MAX_DOC_CHARS,
    OCR_MIN_PAGE_CHARS,
    page_text_quality,
    validate_ocr_text,
)


BASE_RFP_PADDLE_DEVICE = os.getenv("BASE_RFP_PADDLE_DEVICE", "gpu").strip().lower()
BASE_RFP_PADDLE_API_MODEL = os.getenv("BASE_RFP_PADDLE_API_MODEL", "PaddleOCR-VL-1.6").strip()
BASE_RFP_PADDLE_API_REQUEST_TIMEOUT_SECONDS = max(
    30, int(os.getenv("BASE_RFP_PADDLE_API_REQUEST_TIMEOUT_SECONDS", "120"))
)
BASE_RFP_PADDLE_API_POLL_TIMEOUT_SECONDS = max(
    60, int(os.getenv("BASE_RFP_PADDLE_API_POLL_TIMEOUT_SECONDS", "600"))
)
OPENPARSER_BASE_URL = os.getenv("OPENPARSER_BASE_URL", "https://api.openparser.dev").strip().rstrip("/")
OPENPARSER_OCR_MODEL = os.getenv("OPENPARSER_OCR_MODEL", "paddleocr-vl-1.6").strip()
OPENPARSER_MAX_RESPONSE_MB = max(1, min(200, int(os.getenv("OPENPARSER_MAX_RESPONSE_MB", "50"))))
BASE_RFP_OCR_MIN_CONFIDENCE = float(os.getenv("BASE_RFP_OCR_MIN_CONFIDENCE", "0.65"))


@lru_cache(maxsize=1)
def paddle_structure_pipeline() -> Any:
    if os.getenv("PADDLE_OCR_MODE", "local").strip().lower() != "local":
        raise RuntimeError("PADDLE_OCR_MODE=hosted requires BASE_RFP_OCR_PROVIDER=ai and AI_MODEL_OCR")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.getenv("PADDLE_OCR_CACHE_DIR", "/var/cache/procurement/paddle"))
    from paddleocr import PPStructureV3

    return PPStructureV3(
        device=BASE_RFP_PADDLE_DEVICE,
        use_doc_orientation_classify=True,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=os.getenv("PADDLEOCR_USE_SEAL_RECOGNITION", "1") == "1",
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_table_recognition=True,
    )


def ocr_layout_quality(metrics: dict[str, Any], confidence: float, ink_ratio: float) -> tuple[float, bool]:
    sparse_visual_page = bool(
        metrics["characters"] < OCR_MIN_PAGE_CHARS
        and confidence >= BASE_RFP_OCR_MIN_CONFIDENCE
        and ink_ratio < 0.02
    )
    score = confidence if sparse_visual_page else min(float(metrics["score"]), confidence or float(metrics["score"]))
    return score, sparse_visual_page


def ocr_pdf_page_layout_with_paddle(path: Path, zero_based_page: int, page_id: str) -> dict[str, Any]:
    import numpy as np
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        image = np.asarray(document[zero_based_page].render(scale=2.0).to_pil())
    finally:
        document.close()
    grayscale = image[..., :3].mean(axis=2) if image.ndim == 3 else image
    ink_ratio = float((grayscale < 245).mean())
    visual_mean = float(grayscale.mean())
    visual_standard_deviation = float(grayscale.std())
    if visual_standard_deviation < 2.0:
        return {
            "text": "",
            "blocks": [],
            "tables": [],
            "provider": "visual_blank_page",
            "quality_score": 1.0,
            "blank_visual_page": True,
            "quality_metrics": {
                "score": 1.0,
                "characters": 0,
                "words": 0,
                "visual_mean": round(visual_mean, 4),
                "visual_standard_deviation": round(visual_standard_deviation, 4),
                "visual_ink_ratio": round(ink_ratio, 4),
                "blank_visual_page": True,
            },
        }
    use_orientation = ink_ratio >= 0.02
    results = list(
        paddle_structure_pipeline().predict(
            image,
            use_doc_orientation_classify=use_orientation,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_seal_recognition=os.getenv("PADDLEOCR_USE_SEAL_RECOGNITION", "1") == "1",
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_table_recognition=True,
        )
    )
    if not results:
        return {"text": "", "blocks": [], "tables": [], "provider": "paddle_pp_structure_v3", "quality_score": 0.0}
    result = results[0]
    width = float(result.get("width") or image.shape[1])
    height = float(result.get("height") or image.shape[0])
    parsed = [paddle_layout_block_data(item) for item in result.get("parsing_res_list") or []]
    parsed.sort(
        key=lambda item: (
            item.get("block_order") is None,
            coerce_int(item.get("block_order"), 1_000_000),
            coerce_bbox(item.get("block_bbox"))[1] if coerce_bbox(item.get("block_bbox")) else 1_000_000,
        )
    )
    blocks: list[LayoutBlock] = []
    tables: list[LayoutTable] = []
    section_path: list[str] = []
    for item in parsed:
        label = str(item.get("block_label") or "other")
        content = str(item.get("block_content") or "").strip()
        bbox = normalized_bbox(item.get("block_bbox"), width, height)
        if label == "table":
            table = table_from_html(content, f"{page_id}-t{len(tables) + 1}", bbox)
            tables.append(table)
            content = table.markdown
        if not content:
            continue
        if label in {"doc_title", "paragraph_title", "figure_title", "table_title"}:
            block_type = "heading"
            section_path = [clean_text(re.sub(r"<[^>]+>", " ", content))]
        elif label in {"header", "footer", "page_number", "number"}:
            block_type = "footer" if label in {"footer", "page_number", "number"} else "header"
        elif label == "table":
            block_type = "table"
        elif "list" in label:
            block_type = "list_item"
        else:
            block_type = "paragraph"
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=block_type,
                text=clean_text(content),
                reading_order=len(blocks),
                bbox=bbox,
                section_path=list(section_path),
            )
        )
    text = "\n\n".join(block.text for block in blocks)
    ocr = result.get("overall_ocr_res") or {}
    scores = [float(score) for score in ocr.get("rec_scores") or [] if float(score) > 0]
    confidence = sum(scores) / len(scores) if scores else 0.0
    metrics = page_text_quality(text)
    quality_score, sparse_visual_page = ocr_layout_quality(metrics, confidence, ink_ratio)
    metrics.update(
        {
            "ocr_confidence": round(confidence, 4),
            "ocr_lines": len(scores),
            "visual_mean": round(visual_mean, 4),
            "visual_standard_deviation": round(visual_standard_deviation, 4),
            "visual_ink_ratio": round(ink_ratio, 4),
            "sparse_visual_page": sparse_visual_page,
        }
    )
    assign_page_reading_order(blocks, tables)
    return {
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "provider": "paddle_pp_structure_v3",
        "quality_score": round(quality_score, 4),
        "quality_metrics": metrics,
    }


def paddle_api_page_layout(page: Any, page_id: str) -> dict[str, Any]:
    result = page.pruned_result if isinstance(page.pruned_result, dict) else {}
    width = float(result.get("width") or 1)
    height = float(result.get("height") or 1)
    raw_blocks = result.get("parsing_res_list") or []
    if not isinstance(raw_blocks, list) or len(raw_blocks) > 5_000:
        raise ValueError("PaddleOCR API returned an invalid number of layout blocks")
    parsed = [paddle_layout_block_data(item) for item in raw_blocks]
    ordered_items = sum(item.get("block_order") is not None for item in parsed)
    use_provider_order = ordered_items >= max(1, len(parsed) // 2)
    parsed.sort(
        key=lambda item: (
            coerce_int(item.get("block_order"), 1_000_000)
            if use_provider_order
            else coerce_bbox(item.get("block_bbox"))[1]
            if coerce_bbox(item.get("block_bbox"))
            else 1_000_000,
            coerce_bbox(item.get("block_bbox"))[0]
            if coerce_bbox(item.get("block_bbox"))
            else 1_000_000,
        )
    )
    blocks: list[LayoutBlock] = []
    tables: list[LayoutTable] = []
    section_path: list[str] = []
    for item in parsed:
        label = str(item.get("block_label") or "other")
        content = str(item.get("block_content") or "").strip()
        if len(content) > MAX_DOC_CHARS:
            raise ValueError("PaddleOCR API returned an oversized layout block")
        bbox = normalized_bbox(item.get("block_bbox"), width, height)
        if label == "table":
            table = table_from_html(content, f"{page_id}-t{len(tables) + 1}", bbox)
            tables.append(table)
            content = table.markdown
        if not content:
            continue
        if label in {"doc_title", "paragraph_title", "figure_title", "table_title"}:
            block_type = "heading"
            section_path = [clean_text(re.sub(r"<[^>]+>", " ", content))]
        elif label in {"header", "footer", "page_number", "number"}:
            block_type = "footer" if label in {"footer", "page_number", "number"} else "header"
        elif label == "table":
            block_type = "table"
        elif "list" in label:
            block_type = "list_item"
        else:
            block_type = "paragraph"
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=block_type,
                text=clean_text(content),
                reading_order=len(blocks),
                bbox=bbox,
                section_path=list(section_path),
            )
        )
    markdown_text = str(getattr(page, "markdown_text", "") or "")
    if not blocks and markdown_text:
        blocks = layout_blocks_from_text(markdown_text, page_id)
        tables = layout_tables_from_text(markdown_text, page_id)
    text, metrics = validate_ocr_text("\n\n".join(block.text for block in blocks))
    metrics.update(
        {
            "provider_model": BASE_RFP_PADDLE_API_MODEL,
            "layout_blocks": len(blocks),
            "layout_tables": len(tables),
        }
    )
    assign_page_reading_order(blocks, tables)
    return {
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "provider": "paddleocr_api",
        "quality_score": float(metrics["score"]),
        "quality_metrics": metrics,
    }


def ocr_pdf_pages_layout_with_paddle_api(
    path: Path,
    zero_based_pages: list[int],
    page_ids: list[str],
    *,
    usage_attempts: int = 1,
) -> list[dict[str, Any]]:
    if len(zero_based_pages) != len(page_ids) or not zero_based_pages:
        raise ValueError("PaddleOCR API page numbers and page IDs must be non-empty and aligned")
    token = os.getenv("PADDLEOCR_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("PADDLEOCR_ACCESS_TOKEN is required for paddle_api OCR")
    check_provider_budget("paddleocr_document")
    from paddleocr import PaddleOCRClient, PaddleOCRVLOptions
    from pypdf import PdfReader, PdfWriter

    request_timeout = job_bounded_timeout_seconds(BASE_RFP_PADDLE_API_REQUEST_TIMEOUT_SECONDS)
    poll_timeout = job_bounded_timeout_seconds(BASE_RFP_PADDLE_API_POLL_TIMEOUT_SECONDS)
    started = time.monotonic()
    with path.open("rb") as source:
        request_hash = fingerprint(
            {
                "source_sha256": hashlib.file_digest(source, "sha256").hexdigest(),
                "pages": zero_based_pages,
                "model": BASE_RFP_PADDLE_API_MODEL,
            }
        )
    with tempfile.TemporaryDirectory(prefix="paddleocr-api-") as temp_dir:
        selected_path = Path(temp_dir) / "selected-pages.pdf"
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page_number in zero_based_pages:
            writer.add_page(reader.pages[page_number])
        with selected_path.open("wb") as handle:
            writer.write(handle)
        client = PaddleOCRClient(
            token=token,
            base_url=os.getenv("PADDLEOCR_BASE_URL") or None,
            request_timeout=request_timeout,
            poll_timeout=poll_timeout,
            client_platform="procurement-evaluator",
        )
        try:
            result = client.parse_document(
                file_path=str(selected_path),
                model=BASE_RFP_PADDLE_API_MODEL,
                options=PaddleOCRVLOptions(
                    use_doc_orientation_classify=True,
                    use_doc_unwarping=False,
                    use_layout_detection=True,
                    use_chart_recognition=False,
                    use_seal_recognition=os.getenv("PADDLEOCR_USE_SEAL_RECOGNITION", "1") == "1",
                    use_ocr_for_image_block=os.getenv("PADDLEOCR_USE_OCR_FOR_IMAGE_BLOCK", "1") == "1",
                    format_block_content=True,
                    temperature=0.0,
                    prettify_markdown=False,
                    restructure_pages=False,
                    return_markdown_images=False,
                    visualize=False,
                ),
            )
        finally:
            client.close()
    if len(result.pages) != len(page_ids):
        raise RuntimeError(
            f"PaddleOCR API returned {len(result.pages)} pages for {len(page_ids)} requested pages"
        )
    page_ordinals: list[int] = []
    for page in result.pages:
        raw = getattr(page, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        pruned = page.pruned_result if isinstance(page.pruned_result, dict) else {}
        value = next(
            (
                source[key]
                for source in (raw, pruned)
                for key in ("pageIndex", "page_index", "pageNumber", "page_number")
                if key in source
            ),
            None,
        )
        if value is None:
            page_ordinals = []
            break
        page_ordinals.append(coerce_int(value, -1))
    if page_ordinals and page_ordinals not in (
        list(range(len(page_ids))),
        list(range(1, len(page_ids) + 1)),
    ):
        raise RuntimeError("PaddleOCR API returned pages out of order")
    layouts = [paddle_api_page_layout(page, page_id) for page, page_id in zip(result.pages, page_ids)]
    record_provider_call(
        capability="ocr",
        provider="paddleocr",
        model=BASE_RFP_PADDLE_API_MODEL,
        task="paddleocr_document",
        status="succeeded",
        duration_ms=round((time.monotonic() - started) * 1000),
        endpoint=os.getenv("PADDLEOCR_BASE_URL") or None,
        attempts=usage_attempts,
        input_units=len(page_ids),
        output_units=len(layouts),
        unit_name="page",
        request_fingerprint=request_hash,
        response_fingerprint=fingerprint(
            [{"page": page_id, "quality": layout.get("quality_score")} for page_id, layout in zip(page_ids, layouts)]
        ),
    )
    return layouts


def openparser_page_layout(blocks_payload: list[dict[str, Any]], markdown: str, page_id: str) -> dict[str, Any]:
    if len(blocks_payload) > 5_000:
        raise ValueError("OpenParser returned too many layout blocks")
    blocks: list[LayoutBlock] = []
    tables: list[LayoutTable] = []
    confidences: list[float] = []
    section_path: list[str] = []
    for item in blocks_payload:
        kind = str(item.get("kind") or item.get("type") or "text").strip().lower()
        content = str(item.get("text") or item.get("markdown") or item.get("content") or "").strip()
        table_html = str(item.get("table_html") or "").strip()
        if kind == "table" and table_html:
            table = table_from_html(table_html, f"{page_id}-t{len(tables) + 1}", coerce_bbox(item.get("bbox")))
            tables.append(table)
            content = table.markdown or content
        elif kind == "table" and content:
            tables.extend(layout_tables_from_text(content, f"{page_id}-op{len(tables) + 1}"))
        if not content:
            continue
        if len(content) > MAX_DOC_CHARS:
            raise ValueError("OpenParser returned an oversized layout block")
        if kind in {"title", "heading", "document_title", "section_heading", "header", "page_header"}:
            block_type = "header" if kind in {"header", "page_header"} else "heading"
            if block_type == "heading":
                section_path = [clean_text(content)]
        elif kind in {"footer", "page_number"}:
            block_type = "footer"
        elif kind == "table":
            block_type = "table"
        elif kind in {"list", "list_item"}:
            block_type = "list_item"
        else:
            block_type = "paragraph"
        blocks.append(
            LayoutBlock(
                block_id=f"{page_id}-b{len(blocks) + 1}",
                block_type=block_type,
                text=clean_text(content),
                reading_order=len(blocks),
                bbox=coerce_bbox(item.get("bbox")),
                section_path=list(section_path),
            )
        )
        try:
            confidence = float(item.get("confidence"))
            if math.isfinite(confidence) and 0 <= confidence <= 1:
                confidences.append(confidence)
        except (TypeError, ValueError):
            pass
    if not blocks and markdown:
        blocks = layout_blocks_from_text(markdown, page_id)
        tables = layout_tables_from_text(markdown, page_id)
    text, metrics = validate_ocr_text("\n\n".join(block.text for block in blocks))
    confidence = sum(confidences) / len(confidences) if confidences else float(metrics["score"])
    metrics.update(
        {
            "provider_model": OPENPARSER_OCR_MODEL,
            "ocr_confidence": round(confidence, 4),
            "layout_blocks": len(blocks),
            "layout_tables": len(tables),
        }
    )
    assign_page_reading_order(blocks, tables)
    return {
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "provider": "openparser_paddleocr_vl_1_6",
        "quality_score": float(metrics["score"]),
        "quality_metrics": metrics,
    }


def ocr_pdf_pages_layout_with_openparser(
    path: Path,
    zero_based_pages: list[int],
    page_ids: list[str],
    *,
    usage_attempts: int = 1,
) -> list[dict[str, Any]]:
    if len(zero_based_pages) != len(page_ids) or not zero_based_pages:
        raise ValueError("OpenParser page numbers and page IDs must be non-empty and aligned")
    api_key = os.getenv("OPENPARSER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENPARSER_API_KEY is required for openparser OCR")
    check_provider_budget("openparser_document")
    parsed_url = urllib.parse.urlparse(OPENPARSER_BASE_URL)
    if parsed_url.scheme != "https" and parsed_url.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("OPENPARSER_BASE_URL must use HTTPS")
    from pypdf import PdfReader, PdfWriter

    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="openparser-") as temp_dir:
        selected_path = Path(temp_dir) / "selected-pages.pdf"
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page_number in zero_based_pages:
            writer.add_page(reader.pages[page_number])
        with selected_path.open("wb") as handle:
            writer.write(handle)
        pdf_bytes = selected_path.read_bytes()
        request_json = json.dumps(
            {"ocr_model": OPENPARSER_OCR_MODEL, "output_format": "openparser@1"},
            separators=(",", ":"),
        ).encode("utf-8")
        boundary = f"----procurement-{uuid.uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"request\"\r\nContent-Type: application/json\r\n\r\n".encode(),
                request_json,
                b"\r\n",
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"selected-pages.pdf\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
                pdf_bytes,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        with path.open("rb") as handle:
            source_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        idempotency_key = hashlib.sha256(
            f"{source_digest}:{','.join(map(str, zero_based_pages))}:{OPENPARSER_OCR_MODEL}".encode()
        ).hexdigest()
        request_hash = fingerprint(
            {
                "source_sha256": source_digest,
                "pages": zero_based_pages,
                "model": OPENPARSER_OCR_MODEL,
            }
        )
        request = urllib.request.Request(
            f"{OPENPARSER_BASE_URL}/parse",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Idempotency-Key": idempotency_key,
                "Accept": "application/json",
                "User-Agent": "procurement-evaluator/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=job_bounded_timeout_seconds(BASE_RFP_PADDLE_API_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response_body = response.read(OPENPARSER_MAX_RESPONSE_MB * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenParser OCR request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenParser OCR endpoint could not be reached") from exc
    if len(response_body) > OPENPARSER_MAX_RESPONSE_MB * 1024 * 1024:
        raise RuntimeError("OpenParser OCR response exceeded the configured size limit")
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenParser OCR returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("OpenParser OCR returned a non-object response")
    pages_payload = payload.get("pages") or []
    if not isinstance(pages_payload, list):
        raise RuntimeError("OpenParser OCR returned invalid pages")
    page_count = coerce_int(payload.get("page_count"), len(pages_payload) if pages_payload else -1)
    if page_count != len(page_ids):
        raise RuntimeError(f"OpenParser OCR returned {page_count} pages for {len(page_ids)} requested pages")
    raw_blocks = payload.get("blocks") or payload.get("elements") or []
    if not isinstance(raw_blocks, list):
        raise RuntimeError("OpenParser OCR returned invalid blocks")
    page_dimensions = {
        coerce_int(page.get("number"), index + 1): (
            float(page.get("width") or 0),
            float(page.get("height") or 0),
        )
        for index, page in enumerate(pages_payload)
        if isinstance(page, dict)
    }
    normalized_blocks: list[dict[str, Any]] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        locations = item.get("locations") or []
        location = locations[0] if isinstance(locations, list) and locations and isinstance(locations[0], dict) else {}
        page_number = coerce_int(
            item.get("page_number", item.get("page", location.get("page_number", 1))),
            1,
        )
        bbox_value = location.get("bbox") if isinstance(location, dict) else None
        bbox: list[float] = []
        if isinstance(bbox_value, dict):
            width, height = page_dimensions.get(page_number, (0.0, 0.0))
            if width > 0 and height > 0:
                bbox = normalized_bbox(
                    [bbox_value.get("left"), bbox_value.get("top"), bbox_value.get("right"), bbox_value.get("bottom")],
                    width,
                    height,
                )
        confidence_value = item.get("confidence")
        confidence = confidence_value.get("score") if isinstance(confidence_value, dict) else confidence_value
        role = str(item.get("role") or "").strip().lower()
        normalized_blocks.append(
            {
                **item,
                "page_number": page_number,
                "kind": role if role not in {"", "paragraph"} else item.get("kind", "text"),
                "table_html": item.get("table_html") or item.get("html") or "",
                "confidence": confidence,
                "bbox": bbox,
            }
        )
    raw_blocks = normalized_blocks
    block_pages = [
        coerce_int(item.get("page_number", item.get("page", 1)), 1)
        for item in raw_blocks
        if isinstance(item, dict)
    ]
    zero_based_response = bool(block_pages and min(block_pages) == 0)
    grouped: list[list[dict[str, Any]]] = [[] for _ in page_ids]
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        page_number = coerce_int(item.get("page_number", item.get("page", 1)), 1)
        index = page_number if zero_based_response else page_number - 1
        if not 0 <= index < len(grouped):
            raise RuntimeError("OpenParser OCR returned an invalid block page number")
        grouped[index].append(item)
    markdown = str(payload.get("markdown") or payload.get("text") or "")
    layouts = [
        openparser_page_layout(page_blocks, markdown if len(page_ids) == 1 else "", page_id)
        for page_blocks, page_id in zip(grouped, page_ids)
    ]
    record_provider_call(
        capability="ocr",
        provider="openparser",
        model=OPENPARSER_OCR_MODEL,
        task="openparser_document",
        status="succeeded",
        duration_ms=round((time.monotonic() - started) * 1000),
        endpoint=OPENPARSER_BASE_URL,
        attempts=usage_attempts,
        input_units=len(page_ids),
        output_units=len(layouts),
        unit_name="page",
        request_fingerprint=request_hash,
        response_fingerprint=fingerprint(
            [{"page": page_id, "quality": layout.get("quality_score")} for page_id, layout in zip(page_ids, layouts)]
        ),
    )
    return layouts
