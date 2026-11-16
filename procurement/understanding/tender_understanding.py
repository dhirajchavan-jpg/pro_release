"""Tender understanding orchestration with injected compatibility seams."""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from ai_provider import AIProviderFailure
from procurement.understanding.anchor_retrieval import (
    _build_anchor_page_units,
    _enrich_source_roles,
    _selected_pages,
)
from procurement.understanding.field_compiler import RfpUnderstandingSchema
from procurement.understanding.requirement_evidence import _shared_call_budget_exhausted
from procurement.understanding.tender_field_projection import (
    _canonicalize_field_evidence,
    _project_canonical_gem_identity,
    _project_deterministic_tender_controls,
    _project_resolved_tender_summary,
    _project_source_table_ambiguities,
    _refresh_understanding_status,
    _restore_serialized_collection_values,
    field_entries,
)

logger = logging.getLogger("procurement.understanding")
def _run_field_understanding(
    operation_id: str,
    documents: list[Any],
    logical_documents: list[Any],
    selections: list[dict[str, Any]],
    field_schema: dict[str, Any],
    checkpoint_root: Path,
    *,
    source_unit_map: Any,
    source_index: Any,
    embed: Any,
    rerank: Any,
    render_page: Any,
    llm: Any,
    timeout: Any,
    llm_model: str,
    compile_fields: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Resolve only configured fields from locally retrieved candidate units."""
    schema = RfpUnderstandingSchema.model_validate(field_schema)
    units = _enrich_source_roles(
        source_unit_map(_selected_pages(documents, selections)),
        selections,
    )
    if not units:
        raise ValueError("no readable tender source units were available")

    page_index = source_index(documents)

    def image_loader(page_id: str) -> dict[str, str] | None:
        source = page_index.get(page_id)
        if not source:
            return None
        document, page = source
        if document.source_path.suffix.casefold() != ".pdf":
            return None
        return {
            "mime_type": "image/png",
            "data": render_page(
                document.source_path,
                page.page_number - 1,
            ),
        }

    def focused_call(**kwargs: Any) -> dict[str, Any]:
        payload = kwargs["payload"]
        understanding_model = os.getenv("AI_MODEL_UNDERSTANDING", llm_model)
        source_extraction = payload.get("task") == "extract_source_obligations"
        return llm.llm_json(
            kwargs["system"],
            payload,
            model=(os.getenv("AI_MODEL_SOURCE_EXTRACTION", understanding_model)
                   if source_extraction else understanding_model),
            response_schema=kwargs["response_schema"],
            max_output_tokens=kwargs["max_output_tokens"],
            thinking_budget=max(0, int(os.getenv("RFP_FIELD_THINKING_BUDGET", "0"))),
            media_parts=kwargs.get("media_parts"),
            json_retry=os.getenv("RFP_FIELD_JSON_RETRY", "1") == "1",
        )

    anchor_audit: dict[str, Any] = {"enabled": False, "clusters": [], "fact_count": 0}
    if os.getenv("RFP_ANCHOR_PAGES_ENABLED", "1") == "1":
        started = time.monotonic()
        logger.info(
            "ANCHOR job_id=%s stage=selection_started pages=%s",
            operation_id,
            sum(len(document.pages) for document in documents),
        )
        anchor_units, anchor_audit = _build_anchor_page_units(
            operation_id,
            documents,
            schema,
            units,
            call=focused_call,
            image_loader=image_loader,
            checkpoint_root=checkpoint_root,
            model_tag=llm_model,
        )
        units.update(anchor_units)
        logger.info(
            "ANCHOR job_id=%s stage=complete clusters=%s facts=%s degraded=%s duration_seconds=%.3f",
            operation_id,
            len(anchor_audit.get("clusters") or []),
            len(anchor_units),
            bool(anchor_audit.get("degraded")),
            time.monotonic() - started,
        )

    embedding_enabled = os.getenv(
        "RFP_FIELD_EMBEDDING_ENABLED",
        os.getenv("EVIDENCE_EMBEDDING_ENABLED", "0"),
    ) == "1"
    reranker_enabled = os.getenv(
        "RFP_FIELD_RERANKER_ENABLED",
        os.getenv("EVIDENCE_RERANKER_ENABLED", "0"),
    ) == "1"
    semantic_requested = embedding_enabled or reranker_enabled
    retrieval_audit: dict[str, Any] = {
        "mode": "lexical",
        "degraded": False,
        "degraded_reasons": [],
    }
    embedding_vectors: dict[str, list[float]] = {}

    def mark_retrieval_degraded(reason: str) -> None:
        retrieval_audit["degraded"] = True
        reasons = retrieval_audit.setdefault("degraded_reasons", [])
        if reason and reason not in reasons:
            reasons.append(reason)

    def semantic_ranker(
        query: str,
        candidates: list[tuple[str, str]],
    ) -> list[str]:
        ranked = candidates
        if embedding_enabled:
            try:
                missing = [
                    (source_id, text)
                    for source_id, text in ranked
                    if source_id not in embedding_vectors
                ]
                values = [query, *[text[:4000] for _, text in missing]]
                vectors = embed(
                    values,
                    timeout=timeout(),
                )
            except Exception as exc:
                mark_retrieval_degraded(
                    f"embedding_provider_error: {str(exc)[:240]}"
                )
                return [source_id for source_id, _text in candidates]
            if (
                len(vectors) != len(missing) + 1
                or not vectors[0]
                or any(len(vector) != len(vectors[0]) for vector in vectors)
            ):
                mark_retrieval_degraded("embedding_invalid_vector_batch")
                return [source_id for source_id, _text in candidates]
            query_vector = vectors[0]
            embedding_vectors.update(
                (source_id, vector)
                for (source_id, _text), vector in zip(missing, vectors[1:])
            )

            def cosine(vector: list[float]) -> float:
                denominator = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(
                    sum(value * value for value in vector)
                )
                return (
                    sum(left * right for left, right in zip(query_vector, vector)) / denominator
                    if denominator
                    else 0.0
                )

            ranked = [
                candidate
                for _score, candidate in sorted(
                    (
                        (cosine(embedding_vectors[candidate[0]]), candidate)
                        for candidate in ranked
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
            ]
            retrieval_audit["mode"] = "embedding"
        if reranker_enabled and ranked:
            rerank_count = min(
                len(ranked),
                max(1, int(os.getenv("RFP_FIELD_RERANK_CANDIDATES", "12"))),
            )
            try:
                results = rerank(
                    query,
                    [text[:4000] for _, text in ranked[:rerank_count]],
                    top_n=rerank_count,
                    timeout=timeout(),
                )
                if any(bool(item.get("degraded")) for item in results if isinstance(item, dict)):
                    mark_retrieval_degraded("reranker_degraded")
                    return [source_id for source_id, _text in candidates]
                reranked = [ranked[result["index"]] for result in results]
                reranked_ids = {source_id for source_id, _text in reranked}
                ranked = [*reranked, *(item for item in ranked if item[0] not in reranked_ids)]
                retrieval_audit["mode"] = "embedding_reranker" if embedding_enabled else "reranker"
            except Exception as exc:
                mark_retrieval_degraded(
                    f"reranker_provider_error: {str(exc)[:240]}"
                )
                return [source_id for source_id, _text in candidates]
        return [source_id for source_id, _text in ranked]

    if embedding_enabled:
        started = time.monotonic()
        logger.info(
            "EMBEDDING job_id=%s stage=corpus_started source_units=%s",
            operation_id,
            len(units),
        )
        try:
            source_items = list(units.items())
            vectors = embed(
                [str(unit.get("text") or "")[:4000] for _source_id, unit in source_items],
                timeout=timeout(),
            )
            if len(vectors) != len(source_items):
                raise RuntimeError("embedding provider returned an invalid corpus vector batch")
            embedding_vectors.update(
                (source_id, vector)
                for (source_id, _unit), vector in zip(source_items, vectors)
            )
            logger.info(
                "EMBEDDING job_id=%s stage=corpus_complete source_units=%s duration_seconds=%.3f",
                operation_id,
                len(source_items),
                time.monotonic() - started,
            )
        except Exception as exc:
            mark_retrieval_degraded(f"embedding_provider_error: {str(exc)[:240]}")
            embedding_enabled = False
            logger.warning(
                "EMBEDDING job_id=%s stage=corpus_degraded error_type=%s duration_seconds=%.3f",
                operation_id,
                type(exc).__name__,
                time.monotonic() - started,
            )

    field_count = sum(len(section.fields) for section in schema.sections)
    configured_image_calls = os.getenv("RFP_FIELD_MAX_IMAGE_CALLS", "").strip()
    result = compile_fields(
        schema,
        units,
        call=focused_call,
        checkpoint_dir=checkpoint_root / "fields",
        image_loader=image_loader,
        model_tag=llm_model,
        initial_candidates=max(1, int(os.getenv("RFP_FIELD_INITIAL_CANDIDATES", "8"))),
        expanded_candidates=max(1, int(os.getenv("RFP_FIELD_EXPANDED_CANDIDATES", "16"))),
        initial_evidence_chars=max(1000, int(os.getenv("RFP_FIELD_INITIAL_EVIDENCE_CHARS", "9000"))),
        expanded_evidence_chars=max(1000, int(os.getenv("RFP_FIELD_EXPANDED_EVIDENCE_CHARS", "16000"))),
        max_rounds=max(1, int(os.getenv("RFP_FIELD_MAX_ROUNDS", "3"))),
        max_images_per_field=max(0, int(os.getenv("RFP_FIELD_MAX_IMAGES", "3"))),
        max_image_followup_calls=max(
            0,
            int(configured_image_calls) if configured_image_calls else field_count,
        ),
        max_output_tokens=max(128, int(os.getenv("RFP_FIELD_MAX_OUTPUT_TOKENS", "4096"))),
        max_total_calls=int(os.environ["RFP_FIELD_MAX_TOTAL_CALLS"])
        if os.getenv("RFP_FIELD_MAX_TOTAL_CALLS")
        else None,
        amendment_call=focused_call,
        amendment_checkpoint_dir=checkpoint_root / "amendments",
        amendment_evidence_chars=max(4000, int(os.getenv("RFP_AMENDMENT_EVIDENCE_CHARS", "18000"))),
        max_amendment_calls=max(0, int(os.getenv("RFP_AMENDMENT_MAX_CALLS", "24"))),
        max_amendment_images=max(0, int(os.getenv("RFP_AMENDMENT_MAX_IMAGES", "2"))),
        amendment_output_tokens=max(512, int(os.getenv("RFP_AMENDMENT_OUTPUT_TOKENS", "1200"))),
        amendment_deterministic_short_circuit=True,
        field_concurrency=max(1, int(os.getenv("RFP_FIELD_CONCURRENCY", "2"))),
        semantic_ranker=semantic_ranker if semantic_requested else None,
        semantic_pool=max(2, int(os.getenv("RFP_FIELD_SEMANTIC_POOL", "32"))),
    )
    if schema.schema_id == "tender-agent-reviewed-condensed-v2":
        _project_canonical_gem_identity(result, units)
        _project_deterministic_tender_controls(result, units)
        _project_source_table_ambiguities(result, units)
        _canonicalize_field_evidence(result, units)
        _restore_serialized_collection_values(result)
        _project_resolved_tender_summary(result)
        _refresh_understanding_status(result)
    result["audit"]["retrieval"] = retrieval_audit
    result["audit"]["anchor_pages"] = anchor_audit
    if retrieval_audit["degraded"]:
        for field in field_entries(result):
            field.setdefault("audit", {})["retrieval"] = dict(retrieval_audit)
    budget_exhausted = [
        str(field.get("key") or "unknown")
        for field in field_entries(result)
        if _shared_call_budget_exhausted(field)
    ]
    if budget_exhausted:
        raise RuntimeError(
            "tender field understanding call budget exhausted for: "
            + ", ".join(budget_exhausted)
        )
    provider_blocked_reason = str(
        result.get("audit", {}).get("provider_blocked_reason") or ""
    ).strip()
    if provider_blocked_reason:
        lowered = provider_blocked_reason.casefold()
        retryable = not (
            ("spend" in lowered and ("cap" in lowered or "limit" in lowered))
            or any(code in lowered for code in ("http 401", "http 403"))
            or "configuration" in lowered
        )
        raise AIProviderFailure(
            f"tender understanding provider unavailable: {provider_blocked_reason}",
            category="provider_unavailable" if retryable else "configuration",
            retryable=retryable,
        )
    result["audit"]["operation_id"] = operation_id
    if result.get("status") not in {"complete", "partial"}:
        raise RuntimeError("tender field understanding produced no reviewable result")
    from procurement.understanding.source_obligations import extract_source_obligations

    vision_threshold = float(os.getenv("SOURCE_OBLIGATION_VISION_MIN_TEXT_QUALITY", "0.80"))

    def source_page_needs_vision(page_id: str) -> bool:
        source = page_index.get(page_id)
        if not source:
            return False
        document, page = source
        if document.source_path.suffix.casefold() != ".pdf":
            return False
        return bool(
            page.ocr_required
            or "ocr" in page.extraction_method.casefold()
            or page.extraction_quality < vision_threshold
            or page.tables
            or any(block.block_type == "table" for block in page.blocks)
        )

    result["source_obligations"] = extract_source_obligations(
        units, call=focused_call, image_loader=image_loader,
        amendment_state=result.get("amendment_state") or {},
        vision_required=source_page_needs_vision,
        embed=embed,
    )
    if result["source_obligations"]["status"] != "complete":
        result["status"] = "partial"
    return result, units
