"""Evidence routing, scouting, and visual candidate selection."""

import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from ai_provider import embed_texts, rerank_texts

from procurement.domain.schemas import (
    CriteriaEvidenceMap,
    EvidenceMapEntry,
    EvidenceRequirement,
    EvidenceScoutCandidate,
    EvidenceScoutLog,
    EvidenceScoutRecord,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    RequirementMatrix,
    RequirementMatrixItem,
)
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match, unique_items
from procurement.evaluation.criteria import (
    criterion_atomic_requirement_text,
    criterion_retrieval_policy,
)
from procurement.evaluation.evidence_pipeline import (
    BID_EVIDENCE_ROUTE_MAX_PAGES,
    criterion_evidence_query,
    criterion_page_excerpt,
    applicability_signal_score,
    document_type_compatible,
    evidence_marker_score,
    logical_docs_for_page_ids,
    logical_docs_for_page_matches,
    logical_document_summary_score,
    logical_document_text_index,
    match_evidence_pages,
    match_logical_documents,
    specific_evidence_mismatch,
    vendor_content_role_allowed,
    vendor_page_candidates,
)
from procurement.evaluation.facts import canonical_evidence_type
from procurement.evaluation.planning import resolved_evidence_type
from procurement.evaluation.semantic_retrieval import (
    EVIDENCE_EMBEDDING_ENABLED,
    EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND,
    EVIDENCE_RERANKER_ENABLED,
    content_unique_pages,
    dynamic_candidate_window,
    interleave_unique_page_lanes,
    page_content_key,
    prime_semantic_query_scores,
    progressive_local_evidence_search,
    semantic_page_ranking,
    unique_pages,
)
from procurement.platform.job_context import JOB_CONTEXT, check_job_deadline, gemini_call_timeout_seconds
from procurement.platform.llm import AI_VERIFIER_SUPPORTS_VISION, PROCUREMENT_OFFLINE_LLM, llm_json
from procurement.platform.prompts import prompt_text
from procurement.understanding.classification import tender_source_trap
from procurement.understanding.rendering import render_pdf_page_png_b64


logger = logging.getLogger("procurement_evaluator")

BID_SUMMARY_ROUTE_MAX_DOCUMENTS = max(1, int(os.getenv("BID_SUMMARY_ROUTE_MAX_DOCUMENTS", "12")))
EVIDENCE_SCOUT_ENABLED = os.getenv("EVIDENCE_SCOUT_ENABLED", "1") != "0"
EVIDENCE_SCOUT_PAGES_PER_CALL = max(1, int(os.getenv("EVIDENCE_SCOUT_PAGES_PER_CALL", "8")))
EVIDENCE_SCOUT_MAX_PAGE_CHARS = max(500, int(os.getenv("EVIDENCE_SCOUT_MAX_PAGE_CHARS", "5000")))
EVIDENCE_SCOUT_MAX_CANDIDATES = max(1, int(os.getenv("EVIDENCE_SCOUT_MAX_CANDIDATES", "12")))
EVIDENCE_SCOUT_MAX_PAGES_PER_EVIDENCE = max(
    EVIDENCE_SCOUT_PAGES_PER_CALL,
    int(os.getenv("EVIDENCE_SCOUT_MAX_PAGES_PER_EVIDENCE", "48")),
)
EVIDENCE_SCOUT_CONCURRENCY = max(1, int(os.getenv("EVIDENCE_SCOUT_CONCURRENCY", "4")))
EVIDENCE_VISION_ENABLED = os.getenv("EVIDENCE_VISION_ENABLED", "1") != "0"
EVIDENCE_VISION_MAX_PAGES = max(1, int(os.getenv("EVIDENCE_VISION_MAX_PAGES", "4")))
EVIDENCE_VISION_MIN_TEXT_QUALITY = float(os.getenv("EVIDENCE_VISION_MIN_TEXT_QUALITY", "0.80"))


def evidence_mapper_agent(
    matrix: RequirementMatrix,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
    scout_log: EvidenceScoutLog | None = None,
) -> CriteriaEvidenceMap:
    vendor_logical_docs = [doc for doc in logical_documents if doc.upload_side == "vendor"]
    doc_text_by_logical_id = logical_document_text_index(logical_documents, docs)
    page_candidates = vendor_page_candidates(vendor_logical_docs, docs)
    scout_by_evidence = {
        (record.criterion_id, record.evidence_id): record
        for record in (scout_log.records if scout_log else [])
    }
    entries: list[EvidenceMapEntry] = []
    for item in matrix.items:
        for evidence in item.required_evidence:
            page_matches = match_evidence_pages(evidence, item, page_candidates)
            scout_record = scout_by_evidence.get((item.criterion_id, evidence.evidence_id))
            scout_page_ids = [candidate.page_id for candidate in scout_record.candidate_pages] if scout_record else []
            matches = logical_docs_for_page_matches(page_matches, vendor_logical_docs)
            matched_page_ids = unique_items(
                scout_page_ids + [candidate["page"].page_id for _, candidate in page_matches]
            )[:BID_EVIDENCE_ROUTE_MAX_PAGES]
            if scout_page_ids:
                matches = logical_docs_for_page_ids(matched_page_ids, vendor_logical_docs)
            if not matches:
                matches = match_logical_documents(evidence, item, vendor_logical_docs, doc_text_by_logical_id)
                if matches:
                    matched_page_ids = unique_items(
                        matched_page_ids + [page_id for doc in matches for page_id in doc.page_refs[:4]]
                    )[:BID_EVIDENCE_ROUTE_MAX_PAGES]
            status: Literal["found", "missing", "unclear"] = "found" if matches else "missing"
            reason = "Matched vendor page evidence." if matches else "No matching vendor evidence page found."
            if scout_record:
                if matches:
                    reason = f"Evidence scout checked {scout_record.pages_checked} vendor page(s) and found candidate page evidence."
                elif scout_record.status == "unclear":
                    status = "unclear"
                    reason = scout_record.reason
                else:
                    reason = f"Evidence scout checked {scout_record.pages_checked} vendor page(s); no candidate evidence found."
            entries.append(
                EvidenceMapEntry(
                    criterion_id=item.criterion_id,
                    evidence_id=evidence.evidence_id,
                    document_name=evidence.document_name,
                    document_type=evidence.document_type,
                    required=evidence.required,
                    checks=evidence.checks,
                    matched_logical_document_ids=[doc.logical_document_id for doc in matches],
                    matched_page_ids=matched_page_ids,
                    search_coverage=dict(scout_record.search_coverage) if scout_record else {},
                    status=status,
                    reason=reason,
                )
            )
    return CriteriaEvidenceMap(entries=entries)

def evidence_scout_agent(
    matrix: RequirementMatrix,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
) -> EvidenceScoutLog:
    vendor_pages = [
        page
        for document in docs
        if document.record.upload_side == "vendor"
        for page in document.pages
    ]
    if not EVIDENCE_SCOUT_ENABLED or PROCUREMENT_OFFLINE_LLM:
        return EvidenceScoutLog(
            records=[],
            coverage_summary={
                "enabled": EVIDENCE_SCOUT_ENABLED,
                "skipped": "offline_llm" if PROCUREMENT_OFFLINE_LLM else "disabled",
                "vendor_pages": len(vendor_pages),
            },
        )

    work_orders = [
        (item, evidence)
        for item in matrix.items
        for evidence in item.required_evidence
    ]
    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def run_work_order(order: tuple[RequirementMatrixItem, EvidenceRequirement]) -> EvidenceScoutRecord:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        item, evidence = order
        return evidence_scout_record(item, evidence, vendor_pages)

    if EVIDENCE_SCOUT_CONCURRENCY == 1 or len(work_orders) <= 1:
        records = [run_work_order(order) for order in work_orders]
    else:
        with ThreadPoolExecutor(max_workers=min(EVIDENCE_SCOUT_CONCURRENCY, len(work_orders))) as executor:
            futures = [executor.submit(run_work_order, order) for order in work_orders]
            records = [future.result() for future in futures]

    total_work_orders = len(records)
    total_pages = len(vendor_pages)
    blocked = [record for record in records if record.qa_status == "blocked"]
    return EvidenceScoutLog(
        records=records,
        coverage_summary={
            "enabled": True,
            "vendor_pages": total_pages,
            "evidence_work_orders": total_work_orders,
            "expected_scan_records": total_pages * total_work_orders,
            "actual_scan_records": sum(record.pages_checked for record in records),
            "coverage": "complete" if not blocked else "blocked",
            "blocked_records": len(blocked),
        },
    )

def grouped_evidence_scout_agent(
    matrix: RequirementMatrix,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
    semantic_index: dict[str, Any] | None = None,
) -> EvidenceScoutLog:
    """Shortlist bidder evidence locally; the verifier makes the AI decision."""
    vendor_pages = [
        page
        for document in docs
        if document.record.upload_side == "vendor"
        for page in document.pages
    ]
    readable_pages_physical = [
        page
        for page in vendor_pages
        if page.text.strip()
        and vendor_content_role_allowed(page.classification.content_role if page.classification else "unknown")
    ]
    readable_pages = content_unique_pages(readable_pages_physical)
    from procurement.evaluation.search import SearchUnit

    search_units = [
        SearchUnit(
            unit_id=page.page_id,
            document_id=page.document_id,
            page_number=page.page_number,
            text=page.text,
            labels=tuple(
                unique_items(
                    [
                        page.classification.document_type if page.classification else "unknown",
                        *(page.classification.labels if page.classification else []),
                    ]
                )
            ),
            has_image=True,
            image_id=page.page_id,
        )
        for page in readable_pages
    ]
    source_hash = hashlib.sha256(
        "|".join(f"{page.page_id}:{page_content_key(page)}" for page in readable_pages).encode("utf-8")
    ).hexdigest()
    unreadable_page_ids = [page.page_id for page in vendor_pages if not page.text.strip()]
    page_by_id = {page.page_id: page for page in readable_pages}
    vendor_logical_docs = [
        document
        for document in logical_documents
        if document.upload_side == "vendor" and vendor_content_role_allowed(document.content_role)
    ]
    work_orders = [
        (item, evidence)
        for item in matrix.items
        for evidence in item.required_evidence
    ]
    semantic_query_vectors: dict[str, list[float]] = {}
    if semantic_index and work_orders and EVIDENCE_EMBEDDING_ENABLED and not PROCUREMENT_OFFLINE_LLM:
        unique_queries = unique_items([
            criterion_evidence_query(item, evidence)
            for item, evidence in work_orders
        ])
        try:
            vectors = embed_texts(unique_queries, timeout=gemini_call_timeout_seconds())
            if len(vectors) != len(unique_queries):
                raise RuntimeError("embedding provider returned an incomplete criterion-query batch")
            semantic_query_vectors = dict(zip(unique_queries, vectors))
            prime_semantic_query_scores(semantic_index, semantic_query_vectors)
        except Exception as exc:
            logger.warning(
                "Criterion-query embedding batch unavailable; using lexical retrieval: %s",
                str(exc)[:160],
            )
    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def run_work_order(
        order: tuple[RequirementMatrixItem, EvidenceRequirement],
    ) -> EvidenceScoutRecord:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        item, evidence = order
        policy = criterion_retrieval_policy(item, evidence)
        query = criterion_evidence_query(item, evidence)
        query_vector = semantic_query_vectors.get(query, [])
        progressive_pages, progressive_meta = progressive_local_evidence_search(
            item,
            evidence,
            readable_pages,
            search_units=search_units,
            source_hash=source_hash,
        )
        expected_type = resolved_evidence_type(item, evidence)
        summary_documents = {
                document.logical_document_id
                for score, document in sorted(
                    (
                        (logical_document_summary_score(item, evidence, document), document)
                        for document in vendor_logical_docs
                    ),
                    key=lambda pair: pair[0],
                    reverse=True,
                )[:min(BID_SUMMARY_ROUTE_MAX_DOCUMENTS, int(policy["documents"]))]
                if score >= 2
        }
        classified_pages = unique_pages(
                [
                    page_by_id[page_id]
                    for document in vendor_logical_docs
                    if (
                        expected_type not in {"unknown", "other"}
                        and document_type_compatible(expected_type, document.document_type)
                    )
                    or document.logical_document_id in summary_documents
                    for page_id in document.page_refs
                    if page_id in page_by_id
                ]
        )
        route_pages = readable_pages
        route = "classification_and_summary" if summary_documents else (
                "classified_documents" if classified_pages else "text_fallback"
        )
        if classified_pages:
            ranked_documents = sorted(
                    [
                        (
                            logical_document_summary_score(item, evidence, document)
                            + (
                                6
                                if expected_type not in {"unknown", "other"}
                                and document_type_compatible(expected_type, document.document_type)
                                else 0
                            ),
                            document,
                        )
                        for document in vendor_logical_docs
                        if any(page_id in page_by_id for page_id in document.page_refs)
                        and (
                            document.logical_document_id in summary_documents
                            or (
                                expected_type not in {"unknown", "other"}
                                and document_type_compatible(expected_type, document.document_type)
                            )
                        )
                    ],
                    key=lambda pair: pair[0],
                    reverse=True,
            )
            primary_pages = []
            for document_score, document in ranked_documents:
                pages = [
                        page_by_id[page_id]
                        for page_id in document.page_refs
                        if page_id in page_by_id
                ]
                if pages:
                    primary_pages.append(
                        max(
                            pages,
                            key=lambda page: (
                                evidence_scout_page_score(item, evidence, page) + document_score,
                                page.text_chars,
                            ),
                        )
                    )
            classified_ranked = evidence_scout_pages_for_evidence(
                    item,
                    evidence,
                    classified_pages,
                    policy,
                    use_reranker=False,
                    semantic_index=semantic_index,
                    semantic_query_vector=query_vector,
                )
            global_ranked = evidence_scout_pages_for_evidence(
                    item,
                    evidence,
                    readable_pages,
                    policy,
                    use_reranker=str(policy.get("profile") or "") != "objective",
                    semantic_index=semantic_index,
                    semantic_query_vector=query_vector,
                )
            ranked = interleave_unique_page_lanes(
                [primary_pages, progressive_pages, classified_ranked, global_ranked],
                int(policy["final_pages"]),
            )
        else:
            global_ranked = evidence_scout_pages_for_evidence(
                    item,
                    evidence,
                    route_pages,
                    policy,
                    use_reranker=str(policy.get("profile") or "") != "objective",
                    semantic_index=semantic_index,
                    semantic_query_vector=query_vector,
                )
            ranked = interleave_unique_page_lanes(
                [progressive_pages, global_ranked],
                int(policy["final_pages"]),
            )
        found = [
            EvidenceScoutCandidate(
                page_id=page.page_id,
                file_name=page.file_name,
                page_number=page.page_number,
                snippet=clean_text(page.text)[:500],
                why_candidate=(
                    f"Selected from approved classification or its semantic summary for AI verification."
                    if summary_documents
                    else f"Selected from approved {expected_type} classification for AI verification."
                    if classified_pages
                    else "Selected by controlled requirement-term fallback for AI verification."
                ),
                confidence=0.8 if classified_pages else 0.5,
            )
            for page in ranked[:int(policy["final_pages"])]
        ]
        return EvidenceScoutRecord(
            criterion_id=item.criterion_id,
            evidence_id=evidence.evidence_id,
            document_name=evidence.document_name,
            pages_checked=len(route_pages),
            pages_without_text=unreadable_page_ids,
            candidate_pages=found,
            status="candidates_found" if found else ("unclear" if unreadable_page_ids else "missing"),
            reason=(
                f"{len(ranked)} bidder page(s) selected via {route} "
                f"using the {policy['profile']} retrieval profile and passed to evidence verification."
            ),
            search_status=str(progressive_meta.get("status") or ""),
            search_coverage={
                **dict(progressive_meta.get("coverage") or {}),
                # Provider-backed semantic search and verification run in this
                # outer pipeline.  The embedded local lexical planner has those
                # adapters disabled by design, not because a provider failed.
                "degraded_providers": [],
                "physical_readable_pages": len(readable_pages_physical),
                "unique_readable_pages": len(readable_pages),
                "full_corpus_semantic": bool(
                    semantic_index
                    and int(semantic_index.get("pages") or 0) >= len(readable_pages)
                ),
                "selected_page_ids": [page.page_id for page in ranked],
            },
            degradations=list(progressive_meta.get("degradations") or []),
            retrieval_plan=dict(progressive_meta.get("plan") or {}),
        )

    if EVIDENCE_SCOUT_CONCURRENCY == 1 or len(work_orders) <= 1:
        records = [run_work_order(order) for order in work_orders]
    else:
        with ThreadPoolExecutor(max_workers=min(EVIDENCE_SCOUT_CONCURRENCY, len(work_orders))) as executor:
            records = list(executor.map(run_work_order, work_orders))
    return EvidenceScoutLog(
        records=records,
        coverage_summary={
            "enabled": True,
            "mode": "local_shortlist_ai_verify",
            "vendor_pages": len(vendor_pages),
            "evidence_work_orders": len(records),
            "provider_calls": "recorded_in_provider_logs",
            "semantic_index_pages": int((semantic_index or {}).get("pages") or 0),
        },
    )

def evidence_scout_record(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    vendor_pages: list[PageUnit],
) -> EvidenceScoutRecord:
    pages_without_text = [page.page_id for page in vendor_pages if not page.text.strip()]
    candidates: list[EvidenceScoutCandidate] = []
    readable_pages = [page for page in vendor_pages if page.text.strip()]
    scout_pages = evidence_scout_pages_for_evidence(item, evidence, readable_pages)
    ai_checked_page_ids: list[str] = []
    for batch in page_batches(scout_pages, EVIDENCE_SCOUT_PAGES_PER_CALL):
        check_job_deadline()
        ai_checked_page_ids.extend(page.page_id for page in batch)
        candidates.extend(evidence_scout_batch(item, evidence, batch))
        candidates = unique_scout_candidates(candidates)[:EVIDENCE_SCOUT_MAX_CANDIDATES]

    status: Literal["candidates_found", "missing", "unclear"] = "candidates_found" if candidates else "missing"
    qa_status: Literal["accepted", "blocked"] = "accepted"
    if candidates:
        reason = f"Readable vendor pages were locally screened; {len(ai_checked_page_ids)} evidence-like page(s) were AI-inspected and candidate evidence was found."
    elif scout_pages:
        reason = f"Readable vendor pages were locally screened; {len(ai_checked_page_ids)} evidence-like page(s) were AI-inspected and no candidate evidence was found."
    else:
        reason = "Readable vendor pages were locally screened; no evidence-like page was found."
    if pages_without_text and not candidates:
        status = "unclear"
        reason = "No candidate evidence found, but one or more vendor pages had no extractable text."

    return EvidenceScoutRecord(
        criterion_id=item.criterion_id,
        evidence_id=evidence.evidence_id,
        document_name=evidence.document_name,
        pages_checked=len(vendor_pages),
        pages_without_text=pages_without_text,
        not_checked_pages=[],
        candidate_pages=candidates,
        status=status,
        qa_status=qa_status,
        reason=reason,
    )

def evidence_scout_pages_for_evidence(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
    policy: dict[str, int | str] | None = None,
    *,
    use_reranker: bool = True,
    semantic_index: dict[str, Any] | None = None,
    semantic_query_vector: list[float] | None = None,
) -> list[PageUnit]:
    def unique_pages(values: list[PageUnit]) -> list[PageUnit]:
        seen: set[str] = set()
        result: list[PageUnit] = []
        for value in values:
            if value.page_id not in seen:
                seen.add(value.page_id)
                result.append(value)
        return result

    policy = policy or criterion_retrieval_policy(item, evidence)
    scored = [(evidence_scout_page_score(item, evidence, page), page) for page in pages]
    selected = [(score, page) for score, page in scored if score > 0]
    if EVIDENCE_EMBEDDING_ENABLED and not PROCUREMENT_OFFLINE_LLM and not semantic_index:
        selected = sorted(
            scored,
            key=lambda pair: (pair[0], pair[1].text_chars),
            reverse=True,
        )[:int(policy["semantic_pool"])]
    selected.sort(key=lambda pair: (pair[0], pair[1].text_chars), reverse=True)
    lexical_pages = [page for _, page in selected]
    pages_to_rank = list(lexical_pages)
    final_pages = min(int(policy["final_pages"]), len(pages_to_rank))
    query = criterion_evidence_query(item, evidence)
    if semantic_index:
        try:
            semantic_ranked = semantic_page_ranking(
                query,
                pages,
                semantic_index,
                query_vector=semantic_query_vector,
            )
            if semantic_ranked:
                semantic_pages = dynamic_candidate_window(semantic_ranked, policy)
                pages_to_rank = unique_pages([*semantic_pages, *lexical_pages])
                final_pages = min(int(policy["final_pages"]), len(pages_to_rank))
        except Exception as exc:
            logger.warning(
                "Full-corpus bidder retrieval unavailable; preserving lexical ranking: %s",
                str(exc)[:160],
            )
    if (
        EVIDENCE_EMBEDDING_ENABLED
        and pages_to_rank
        and not PROCUREMENT_OFFLINE_LLM
        and not semantic_index
    ):
        try:
            vectors = embed_texts(
                [query] + [page.text[:EVIDENCE_SCOUT_MAX_PAGE_CHARS] for page in pages_to_rank],
                timeout=gemini_call_timeout_seconds(),
            )
            if len(vectors) != len(pages_to_rank) + 1 or not vectors[0] or any(len(vector) != len(vectors[0]) for vector in vectors):
                raise RuntimeError("embedding provider returned an invalid vector batch")
            query_vector = vectors[0]

            def cosine(vector: list[float]) -> float:
                denominator = math.sqrt(sum(value * value for value in query_vector)) * math.sqrt(sum(value * value for value in vector))
                return sum(left * right for left, right in zip(query_vector, vector)) / denominator if denominator else 0.0

            pages_to_rank = [
                page
                for _score, page in sorted(
                    ((cosine(vector), page) for vector, page in zip(vectors[1:], pages_to_rank)),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
            ]
        except Exception as exc:
            logger.warning(
                "Embedding retrieval unavailable; preserving deterministic lexical ranking: %s",
                str(exc)[:160],
            )
    if use_reranker and EVIDENCE_RERANKER_ENABLED and pages_to_rank and not PROCUREMENT_OFFLINE_LLM:
        rerank_count = min(int(policy["rerank"]), len(pages_to_rank))
        ranked = rerank_texts(
            query,
            [
                page.text[:EVIDENCE_SCOUT_MAX_PAGE_CHARS]
                for page in pages_to_rank[:rerank_count]
            ],
            top_n=rerank_count,
            timeout=gemini_call_timeout_seconds(),
        )
        # A reranker may improve order, but it must never erase the strongest
        # embedding/BM25 candidates. Give the judge the union of both lanes.
        selected: list[PageUnit] = []
        seen: set[str] = set()
        for page in [
            *[pages_to_rank[result["index"]] for result in ranked[:final_pages]],
            *pages_to_rank[:final_pages],
            *lexical_pages[:final_pages],
        ]:
            if page.page_id not in seen:
                selected.append(page)
                seen.add(page.page_id)
        return selected
    return unique_pages([*pages_to_rank[:final_pages], *lexical_pages[:final_pages]])

def evidence_scout_page_score(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    page: PageUnit,
) -> int:
    text = page.text[:EVIDENCE_SCOUT_MAX_PAGE_CHARS]
    query = criterion_evidence_query(item, evidence)
    score = len(keyword_tokens(query) & keyword_tokens(text))
    score += evidence_marker_score(evidence, text)
    applicability_score = applicability_signal_score(item, text)
    score += applicability_score
    if page.classification:
        if page.classification.content_role == "reference":
            score -= 4
        if document_type_compatible(evidence.document_type, page.classification.document_type, text):
            score += 4
    if specific_evidence_mismatch(evidence, text) and not applicability_score:
        score -= 4
    if tender_source_trap(text):
        score -= 6
    return score

def evidence_scout_batch(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
) -> list[EvidenceScoutCandidate]:
    data = llm_json(
        prompt_text("evidence_scout", "You are a procurement evidence scout. Inspect every provided vendor page for the requested evidence. Return only JSON."),
        {
            "task": "evidence_page_scout",
            "rules": [
                "Inspect every page in this batch against the evidence requirement.",
                "Return candidate pages even when the evidence is partial, embedded inside a long document, or named differently.",
                "Do not use filenames alone.",
                "Do not treat copied tender instructions as bidder evidence.",
                "If a page may contain relevant proof, include the page_id, a short snippet, and why it may satisfy the requirement.",
                "If no page is relevant, return an empty candidate_pages array.",
            ],
            "criterion": model_dump(item),
            "evidence_requirement": model_dump(evidence),
            "pages": [
                {
                    "page_id": page.page_id,
                    "file_name": page.file_name,
                    "page_number": page.page_number,
                    "text": page.text[:EVIDENCE_SCOUT_MAX_PAGE_CHARS],
                }
                for page in pages
            ],
            "json_shape": {
                "candidate_pages": [
                    {
                        "page_id": "one of the provided page ids",
                        "snippet": "short exact snippet from page",
                        "why_candidate": "why this page may contain the requested evidence",
                        "confidence": "0 to 1",
                    }
                ]
            },
        },
    )
    by_id = {page.page_id: page for page in pages}
    result: list[EvidenceScoutCandidate] = []
    raw_candidates = data.get("candidate_pages") if isinstance(data, dict) else []
    if not isinstance(raw_candidates, list):
        return []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        page = by_id.get(str(raw.get("page_id") or ""))
        if not page:
            continue
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        result.append(
            EvidenceScoutCandidate(
                page_id=page.page_id,
                file_name=page.file_name,
                page_number=page.page_number,
                snippet=clean_text(str(raw.get("snippet") or ""))[:500],
                why_candidate=clean_text(str(raw.get("why_candidate") or ""))[:500],
                confidence=max(0.0, min(1.0, confidence)),
            )
        )
    return result

def page_batches(pages: list[PageUnit], size: int) -> list[list[PageUnit]]:
    return [pages[index : index + size] for index in range(0, len(pages), size)]

def unique_scout_candidates(candidates: list[EvidenceScoutCandidate]) -> list[EvidenceScoutCandidate]:
    result: list[EvidenceScoutCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.page_id in seen:
            continue
        seen.add(candidate.page_id)
        result.append(candidate)
    return result

def visual_evidence_reasons(page: PageUnit, entry: EvidenceMapEntry) -> list[str]:
    if not EVIDENCE_VISION_ENABLED or not AI_VERIFIER_SUPPORTS_VISION:
        return []
    reasons: list[str] = []
    if page.extraction_quality < EVIDENCE_VISION_MIN_TEXT_QUALITY:
        reasons.append("low_text_quality")
    layout_sensitive_checks = set(entry.checks) - {
        "presence",
        "bidder_name_match",
        "date_valid",
    }
    if layout_sensitive_checks and (
        page.tables or any(block.block_type == "table" for block in page.blocks)
    ):
        reasons.append("table_or_layout_heavy")
    document_type = canonical_evidence_type(entry.document_type)
    classified_type = page.classification.document_type if page.classification else "unknown"
    certificate_candidate = bool(
        evidence_marker_score(
            EvidenceRequirement(
                evidence_id=entry.evidence_id,
                document_name=entry.document_name,
                document_type=entry.document_type,
                checks=entry.checks,
            ),
            page.text,
        )
        or document_type_compatible(entry.document_type, classified_type, page.text)
    )
    if certificate_candidate and document_type in {
        "mse_udyam_or_startup_exemption",
        "turnover_networth_ca_certificate",
        "audited_financials_or_itr",
        "quality_certificate",
        "calibration_certificate",
        "oem_authorization",
        "emd_or_bid_security",
        "gst_pan_cin_statutory",
        "poa_or_board_authorization",
    }:
        reasons.append("certificate_or_identity_evidence")
    if any(
        marker in check.lower()
        for check in entry.checks
        for marker in ("sign", "stamp", "seal", "letterhead")
    ):
        reasons.append("visual_authenticity_check")
    return unique_items(reasons)

def evidence_vision_media(
    entry: EvidenceMapEntry,
    page_source_by_id: dict[str, tuple[ExtractedDocument, PageUnit]],
    page_ids: list[str] | None = None,
    *,
    force_reason: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    media_parts: list[dict[str, Any]] = []
    visual_checks: list[dict[str, Any]] = []
    for page_id in unique_items(page_ids or entry.matched_page_ids):
        source = page_source_by_id.get(page_id)
        if not source:
            continue
        document, page = source
        reasons = visual_evidence_reasons(page, entry)
        if force_reason and EVIDENCE_VISION_ENABLED and AI_VERIFIER_SUPPORTS_VISION:
            reasons = unique_items([*reasons, force_reason])
        if not reasons or not document.source_path or document.source_path.suffix.lower() != ".pdf":
            continue
        try:
            image_b64 = render_pdf_page_png_b64(document.source_path, page.page_number - 1)
        except Exception as exc:
            visual_checks.append(
                {
                    "page_id": page.page_id,
                    "page_number": page.page_number,
                    "mode": "document_vision",
                    "status": "render_error",
                    "reason": str(exc)[:240],
                }
            )
            continue
        media_parts.extend(
            [
                {
                    "text": (
                        f"Bidder evidence image: page_id={page.page_id}, "
                        f"file_name={page.file_name}, page_number={page.page_number}. "
                        f"Inspect because: {', '.join(reasons)}."
                    )
                },
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]
        )
        visual_checks.append(
            {
                "page_id": page.page_id,
                "page_number": page.page_number,
                "mode": "document_vision",
                "status": "inspected",
                "reason": ", ".join(reasons),
            }
        )
        if len(visual_checks) >= EVIDENCE_VISION_MAX_PAGES:
            break
    return media_parts, visual_checks

def recovery_candidate_pages(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
    seen_page_ids: set[str],
    semantic_index: dict[str, Any] | None,
) -> list[PageUnit]:
    unseen = [page for page in pages if page.page_id not in seen_page_ids]
    if not unseen:
        return []
    policy = criterion_retrieval_policy(item, evidence)
    query = criterion_evidence_query(item, evidence)
    try:
        ranked = semantic_page_ranking(query, unseen, semantic_index) if semantic_index else []
    except Exception as exc:
        logger.warning("Criterion recovery semantic search failed; using lexical ranking: %s", str(exc)[:160])
        ranked = []
    if not ranked:
        ranked = sorted(
            ((float(evidence_scout_page_score(item, evidence, page)), page) for page in unseen),
            key=lambda pair: (pair[0], pair[1].text_chars),
            reverse=True,
        )
    window = dynamic_candidate_window(ranked, policy, recovery=True)
    if EVIDENCE_RERANKER_ENABLED and window and not PROCUREMENT_OFFLINE_LLM:
        try:
            reranked = rerank_texts(
                query,
                [criterion_page_excerpt(page, item, evidence) for page in window],
                top_n=len(window),
                timeout=gemini_call_timeout_seconds(),
            )
            window = [window[result["index"]] for result in reranked]
        except Exception as exc:
            logger.warning("Criterion recovery reranker failed; preserving semantic order: %s", str(exc)[:160])
    return window

def exhaustive_recovery_candidate_pages(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
    seen_page_ids: set[str],
    *,
    max_pages: int,
) -> list[PageUnit]:
    """Inspect residual pages that still have an explicit artifact or criterion signal.

    Semantic search covers the complete readable corpus, but it is deliberately
    approximate.  Before declaring evidence absent, this deterministic lane
    checks every remaining page for exact requirement, artifact, classification,
    and procurement-dictionary signals.  It is a final safety net, not a second
    unbounded retrieval loop.
    """
    candidates: list[tuple[int, int, PageUnit]] = []
    for page in pages:
        if page.page_id in seen_page_ids:
            continue
        marker_score = evidence_marker_score(evidence, page.text)
        score = evidence_scout_page_score(item, evidence, page)
        compatible = bool(
            page.classification
            and document_type_compatible(
                evidence.document_type,
                page.classification.document_type,
                page.text,
            )
        )
        if marker_score > 0 or score > 0 or compatible:
            candidates.append((marker_score, score, page))
    candidates.sort(
        key=lambda value: (
            value[0] > 0,
            value[0],
            value[1],
            value[2].text_chars,
            value[2].page_id,
        ),
        reverse=True,
    )
    return [page for _marker, _score, page in candidates[:max(1, max_pages)]]

def full_corpus_search_completed(
    entry: EvidenceMapEntry,
    pages: list[PageUnit],
    semantic_index: dict[str, Any] | None,
) -> bool:
    unique_page_count = len(content_unique_pages(pages))
    indexed_page_count = int((semantic_index or {}).get("pages") or 0)
    return bool(
        unique_page_count
        and entry.search_coverage.get("full_corpus_semantic")
        and indexed_page_count >= unique_page_count
    )
