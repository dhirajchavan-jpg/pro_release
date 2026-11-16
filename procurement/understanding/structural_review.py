"""Visual adjudication for ambiguous tender section boundaries."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from procurement.domain.schemas import (
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    StructuralPacket,
)
from procurement.domain.text import clean_text
from procurement.platform.job_context import JOB_CONTEXT
from procurement.platform.files import write_json_atomic
from procurement.platform.llm import (
    GEMINI_MODEL_FAST,
    GEMINI_THINKING_BUDGET,
    gemini_api_key,
    parse_json_object,
    post_gemini_content,
)
from procurement.platform.prompts import prompt_text
from procurement.understanding.rendering import pdf_page_base64
from procurement.understanding.structure import (
    normalize_structural_page_reviews,
    reconstruct_document_structure,
    structural_heading_parts,
    structural_page_review_response_schema,
    structural_review_candidates,
)


BASE_RFP_STRUCTURAL_REVIEW_MAX_PAGES = max(
    1, int(os.getenv("BASE_RFP_STRUCTURAL_REVIEW_MAX_PAGES", "12"))
)
BASE_RFP_STRUCTURAL_REVIEW_CONCURRENCY = max(
    1, int(os.getenv("BASE_RFP_STRUCTURAL_REVIEW_CONCURRENCY", "3"))
)
BASE_RFP_STRUCTURAL_REVIEW_VERSION = "structural-page-review-v1"
BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED = os.getenv(
    "BASE_RFP_STRUCTURAL_ADJUDICATION_ENABLED", "0"
) == "1"


def review_structural_page_with_gemini(
    document: ExtractedDocument,
    page: PageUnit,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    api_key = gemini_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is required for structural adjudication")
    if not document.source_path or document.source_path.suffix.lower() != ".pdf":
        raise RuntimeError(f"visual structural review requires a PDF source: {document.record.file_name}")
    prompt = prompt_text(
        "structural_page_review",
        "You are adjudicating structural decisions from an already-extracted procurement page. "
        "Review only the listed block IDs and decide whether each block truly opens a document section. "
        "Candidates may be existing headings that are false or body blocks that the local parser may have missed. "
        "Use the rendered page, typography, indentation, surrounding sentence, table boundaries, and numbering context. "
        "Inline annexure references, sentence continuations, table content, list items, headers, and footers are not section headings. "
        "A true heading must visibly and semantically introduce the content that follows. "
        "Do not transcribe, summarize, or rewrite the page. Return exactly one concise review for every listed block ID.",
    )
    payload = {
        "task": "structural_page_adjudication",
        "document": document.record.file_name,
        "page_number": page.page_number,
        "candidate_blocks": candidate.get("blocks") or [],
    }
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": f"{prompt}\n\nCandidate data:\n{json.dumps(payload, ensure_ascii=True)}"},
                    {"inline_data": {"mime_type": "application/pdf", "data": pdf_page_base64(document.source_path, page.page_number - 1)}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": structural_page_review_response_schema(),
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
        },
    }
    data = parse_json_object(post_gemini_content(api_key, GEMINI_MODEL_FAST, body, task="structural_page_adjudication"))
    reviews = normalize_structural_page_reviews(data, candidate)
    if not reviews:
        raise RuntimeError(f"Gemini returned no valid structural reviews for {page.page_id}")
    return reviews

def apply_structural_page_reviews(page: PageUnit, candidate: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, int]:
    blocks = {block.block_id: block for block in page.blocks}
    candidate_by_id = {item["block_id"]: item for item in candidate.get("blocks") or []}
    applied = 0
    changed = 0
    for review in reviews:
        block = blocks.get(review["block_id"])
        candidate_block = candidate_by_id.get(review["block_id"])
        if block is None or candidate_block is None:
            continue
        was_heading = bool(candidate_block.get("current_is_heading"))
        is_heading = bool(review["is_section_heading"])
        canonical_number = review["canonical_number"]
        canonical_title = review["canonical_title"]
        heading_level = review["heading_level"]
        if is_heading and (not canonical_title or heading_level < 1):
            fallback = structural_heading_parts(block.text, font_size=block.font_size, body_size=0.0, is_bold=block.is_bold)
            if fallback:
                canonical_number = canonical_number or fallback[0]
                canonical_title = canonical_title or fallback[1]
                heading_level = heading_level or fallback[2]
            else:
                canonical_title = canonical_title or clean_text(block.text)
                heading_level = heading_level or 1
        block.structural_review = {
            **review,
            "status": "applied",
            "provider": "gemini_structural_adjudicator",
            "version": BASE_RFP_STRUCTURAL_REVIEW_VERSION,
            "candidate_risk_score": candidate_block.get("risk_score", 0),
            "candidate_signals": candidate_block.get("signals") or [],
            "canonical_number": canonical_number,
            "canonical_title": canonical_title,
            "heading_level": heading_level,
        }
        applied += 1
        changed += was_heading != is_heading
    return {"applied": applied, "changed": changed}

def run_structural_adjudication(
    documents: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
    structure: StructuralPacket | None = None,
    checkpoint_dir: Path | None = None,
    max_pages: int | None = None,
) -> StructuralPacket:
    initial = structure or reconstruct_document_structure(documents, logical_documents)
    candidates = structural_review_candidates(documents, initial)
    limit = max(1, max_pages or BASE_RFP_STRUCTURAL_REVIEW_MAX_PAGES)
    selected = candidates[:limit]
    document_lookup = {document.record.document_id: document for document in documents}
    page_lookup = {page.page_id: page for document in documents for page in document.pages}
    api_key = gemini_api_key()
    if not api_key:
        initial.adjudication = {
            "status": "unavailable",
            "reason": "The configured AI provider is unavailable",
            "candidate_pages": len(candidates),
            "reviewed_pages": 0,
            "remaining_high_risk_pages": len(candidates),
        }
        initial.metrics["structural_review_pages"] = 0
        initial.metrics["structural_risk_pages_remaining"] = len(candidates)
        return initial
    if not selected:
        initial.adjudication = {
            "status": "not_needed",
            "candidate_pages": 0,
            "reviewed_pages": 0,
            "remaining_high_risk_pages": 0,
        }
        initial.metrics["structural_review_pages"] = 0
        initial.metrics["structural_risk_pages_remaining"] = 0
        return initial

    document_hashes: dict[str, str] = {}
    for candidate in selected:
        document = document_lookup.get(candidate["document_id"])
        if not document or not document.source_path or not document.source_path.is_file():
            continue
        if document.record.document_id not in document_hashes:
            with document.source_path.open("rb") as handle:
                document_hashes[document.record.document_id] = hashlib.file_digest(handle, "sha256").hexdigest()

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def review_one(candidate: dict[str, Any]) -> dict[str, Any]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        document = document_lookup.get(candidate["document_id"])
        page = page_lookup.get(candidate["page_id"])
        if not document or not page:
            return {"candidate": candidate, "status": "failed", "error": "source page unavailable"}
        if not document.source_path or document.source_path.suffix.lower() != ".pdf":
            return {"candidate": candidate, "status": "failed", "error": "visual review currently supports PDF pages"}
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version": BASE_RFP_STRUCTURAL_REVIEW_VERSION,
                    "model": GEMINI_MODEL_FAST,
                    "candidate": candidate,
                },
                ensure_ascii=True,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cache_path = None
        document_hash = document_hashes.get(document.record.document_id)
        if checkpoint_dir and document_hash:
            cache_path = checkpoint_dir / document_hash / f"page-{page.page_number}.json"
            if cache_path.is_file():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    if cached.get("fingerprint") == fingerprint:
                        reviews = normalize_structural_page_reviews(cached, candidate)
                        if reviews:
                            return {"candidate": candidate, "status": "ok", "reviews": reviews, "cache_hit": True}
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        try:
            reviews = review_structural_page_with_gemini(document, page, candidate)
            if cache_path:
                write_json_atomic(
                    cache_path,
                    {
                        "fingerprint": fingerprint,
                        "version": BASE_RFP_STRUCTURAL_REVIEW_VERSION,
                        "model": GEMINI_MODEL_FAST,
                        "reviews": reviews,
                    },
                )
            return {"candidate": candidate, "status": "ok", "reviews": reviews, "cache_hit": False}
        except Exception as exc:
            return {"candidate": candidate, "status": "failed", "error": str(exc)[:1000], "cache_hit": False}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(BASE_RFP_STRUCTURAL_REVIEW_CONCURRENCY, len(selected))) as executor:
        futures = [executor.submit(review_one, candidate) for candidate in selected]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (-item["candidate"]["risk_score"], item["candidate"]["page_number"]))

    applied_blocks = 0
    changed_blocks = 0
    page_audit: list[dict[str, Any]] = []
    for result in results:
        candidate = result["candidate"]
        page = page_lookup.get(candidate["page_id"])
        counts = {"applied": 0, "changed": 0}
        if result["status"] == "ok" and page is not None:
            counts = apply_structural_page_reviews(page, candidate, result.get("reviews") or [])
            applied_blocks += counts["applied"]
            changed_blocks += counts["changed"]
        page_audit.append(
            {
                "page_id": candidate["page_id"],
                "document_id": candidate["document_id"],
                "page_number": candidate["page_number"],
                "risk_score": candidate["risk_score"],
                "candidate_block_ids": [item["block_id"] for item in candidate.get("blocks") or []],
                "status": result["status"],
                "cache_hit": bool(result.get("cache_hit")),
                "applied_blocks": counts["applied"],
                "changed_blocks": counts["changed"],
                "error": result.get("error", ""),
            }
        )

    final = reconstruct_document_structure(documents, logical_documents) if applied_blocks else initial
    remaining = structural_review_candidates(documents, final)
    final.adjudication = {
        "status": "complete" if not remaining else "review_required",
        "version": BASE_RFP_STRUCTURAL_REVIEW_VERSION,
        "model": GEMINI_MODEL_FAST,
        "candidate_pages": len(candidates),
        "selected_pages": len(selected),
        "reviewed_pages": sum(item["status"] == "ok" for item in page_audit),
        "cache_hits": sum(item["cache_hit"] for item in page_audit),
        "applied_blocks": applied_blocks,
        "changed_blocks": changed_blocks,
        "remaining_high_risk_pages": len(remaining),
        "remaining_pages": [
            {
                "page_id": item["page_id"],
                "page_number": item["page_number"],
                "risk_score": item["risk_score"],
                "candidate_block_ids": [block["block_id"] for block in item.get("blocks") or []],
            }
            for item in remaining[:50]
        ],
        "pages": page_audit,
    }
    final.metrics["structural_review_pages"] = final.adjudication["reviewed_pages"]
    final.metrics["structural_review_changed_blocks"] = changed_blocks
    final.metrics["structural_risk_pages_remaining"] = len(remaining)
    if remaining:
        final.unresolved.append(
            {
                "reason": "high_risk_structure_remaining_after_targeted_review",
                "page_count": len(remaining),
                "page_ids": [item["page_id"] for item in remaining[:100]],
            }
        )
    return final
