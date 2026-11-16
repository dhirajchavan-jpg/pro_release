"""Reusable lexical and semantic retrieval for bidder pages."""

import hashlib
import logging
import math
import os
import re
from typing import Any

import numpy as np

from ai_provider import embed_texts

from procurement.domain.schemas import EvidenceRequirement, PageUnit, RequirementMatrixItem
from procurement.domain.text import clean_text, normalize_for_match, unique_items
from procurement.evaluation.criteria import (
    criterion_atomic_requirement_text,
    criterion_retrieval_policy,
)
from procurement.platform.job_context import gemini_call_timeout_seconds
from procurement.platform.llm import PROCUREMENT_OFFLINE_LLM
from procurement.understanding.source_units import base_rfp_source_unit_map


logger = logging.getLogger("procurement_evaluator")

EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND = max(1, int(os.getenv("EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND", "12")))
EVIDENCE_EMBEDDING_CHUNK_CHARS = max(1000, int(os.getenv("EVIDENCE_EMBEDDING_CHUNK_CHARS", "4000")))
EVIDENCE_EMBEDDING_CHUNK_OVERLAP = max(0, int(os.getenv("EVIDENCE_EMBEDDING_CHUNK_OVERLAP", "400")))
EVIDENCE_RERANKER_ENABLED = os.getenv("EVIDENCE_RERANKER_ENABLED", "0") == "1"
EVIDENCE_EMBEDDING_ENABLED = os.getenv("EVIDENCE_EMBEDDING_ENABLED", "0") == "1"


def progressive_local_evidence_search(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
    *,
    search_units: list[Any] | None = None,
    source_hash: str = "",
) -> tuple[list[PageUnit], dict[str, Any]]:
    """Bounded corpus search; classification is a ranking hint, never a filter."""
    from procurement.evaluation.search import (
        CriterionSpec,
        ProgressiveCriterionSearchPlanner,
        ProviderCapabilities,
        SearchBudget,
        SearchUnit,
    )

    query = " ".join(
        [
            criterion_atomic_requirement_text(item),
            item.pass_rule,
            evidence.document_name,
            evidence.document_type,
            " ".join(evidence.checks),
        ]
    )
    exact_terms = unique_items(
        re.findall(r"\b[A-Z]{5}\d{4}[A-Z]\b", query.upper())
        + re.findall(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", query.upper())
        + re.findall(r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b", query.upper())
    )
    policy = criterion_retrieval_policy(item, evidence)
    classification_hints = unique_items(
        [
            evidence.document_type,
            *list(evidence.accepted_document_types or []),
        ]
    )
    visual = bool(evidence.visual_checks) or any(
        marker in normalize_for_match(query)
        for marker in ("signature", "stamp", "seal", "photograph", "image", "signed")
    )
    spec = CriterionSpec(
        criterion_id=f"{item.criterion_id}:{evidence.evidence_id}",
        requirement=query,
        objective=(
            "identifier"
            if exact_terms or any(marker in f" {normalize_for_match(query)} " for marker in (" pan ", " gstin ", " cin ", " udyam "))
            else str(policy.get("profile") or "standard")
        ),
        exact_terms=tuple(exact_terms),
        classification_hints=tuple(classification_hints),
        artifact_labels=tuple(evidence.accepted_document_types or []),
        broad=str(policy.get("profile") or "") == "broad",
        visual=visual,
        multi_document=str(policy.get("profile") or "") == "broad",
        min_evidence=2 if str(policy.get("profile") or "") == "broad" else 1,
    )
    units = search_units or [
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
        for page in pages
    ]
    planner = ProgressiveCriterionSearchPlanner(
        SearchBudget(
            max_candidates_per_stage=max(8, int(policy["rerank"])),
            max_unseen_pages=max(len(units), int(policy["semantic_pool"])),
            max_image_pages=max(8, int(policy["final_pages"])),
            max_vlm_pages=max(1, int(policy["final_pages"])),
            max_total_candidates=max(len(units), int(policy["semantic_pool"])),
        ),
        capabilities=ProviderCapabilities(
            embeddings=False,
            reranker=False,
            image_embeddings=False,
            vlm=False,
        ),
    )
    result = planner.search(spec, units, source_hash=source_hash)
    by_id = {page.page_id: page for page in pages}
    selected = [by_id[page_id] for page_id in result.matched_unit_ids if page_id in by_id]
    return selected, {
        "status": result.status.value,
        "coverage": result.coverage.to_dict(),
        # Provider lanes execute in the main router/verifier, so the local-only
        # planner must not report them as provider outages.
        "degradations": [],
        "plan": planner.plan(spec).to_dict(),
    }

def bidder_semantic_index(pages: list[PageUnit]) -> dict[str, Any] | None:
    """Embed every readable bidder source unit once for reuse by all criteria."""
    if not EVIDENCE_EMBEDDING_ENABLED or PROCUREMENT_OFFLINE_LLM:
        return None
    entries: list[dict[str, Any]] = []
    step = max(1, EVIDENCE_EMBEDDING_CHUNK_CHARS - EVIDENCE_EMBEDDING_CHUNK_OVERLAP)
    for unit in base_rfp_source_unit_map(content_unique_pages(pages)).values():
        text = clean_text(str(unit.get("text") or ""))
        if not text:
            continue
        for offset in range(0, len(text), step):
            chunk = text[offset : offset + EVIDENCE_EMBEDDING_CHUNK_CHARS]
            if not chunk:
                continue
            entries.append(
                {
                    "page_id": str(unit.get("page_id") or ""),
                    "text": chunk,
                }
            )
            if offset + EVIDENCE_EMBEDDING_CHUNK_CHARS >= len(text):
                break
    if not entries:
        return None
    try:
        vectors = embed_texts(
            [entry["text"] for entry in entries],
            timeout=gemini_call_timeout_seconds(),
        )
        if len(vectors) != len(entries) or not vectors or any(
            len(vector) != len(vectors[0]) for vector in vectors
        ):
            raise RuntimeError("embedding provider returned an invalid bidder index")
    except Exception as exc:
        logger.warning("Bidder semantic index unavailable; using lexical retrieval: %s", str(exc)[:160])
        return None
    for entry, vector in zip(entries, vectors):
        entry["vector"] = vector
        entry["norm"] = math.sqrt(sum(value * value for value in vector))
    matrix = np.asarray(vectors, dtype=np.float32)
    return {
        "entries": entries,
        "pages": len({entry["page_id"] for entry in entries}),
        "_matrix": matrix,
        "_norms": np.linalg.norm(matrix, axis=1),
        "_page_ids": [entry["page_id"] for entry in entries],
    }


def prime_semantic_query_scores(
    semantic_index: dict[str, Any] | None,
    query_vectors: dict[str, list[float]],
) -> None:
    """Compute every criterion/page cosine score in one vectorized pass."""
    if not semantic_index or not query_vectors:
        return
    matrix = semantic_index.get("_matrix")
    norms = semantic_index.get("_norms")
    page_ids = semantic_index.get("_page_ids")
    if not isinstance(matrix, np.ndarray) or not isinstance(norms, np.ndarray) or not page_ids:
        return
    queries = list(query_vectors)
    query_matrix = np.asarray([query_vectors[query] for query in queries], dtype=np.float32)
    if query_matrix.ndim != 2 or matrix.ndim != 2 or query_matrix.shape[1] != matrix.shape[1]:
        return
    denominators = np.linalg.norm(query_matrix, axis=1)[:, None] * norms[None, :]
    similarities = np.divide(
        query_matrix @ matrix.T,
        denominators,
        out=np.zeros((len(queries), matrix.shape[0]), dtype=np.float32),
        where=denominators != 0,
    )
    cache = semantic_index.setdefault("_query_scores", {})
    for row, query in enumerate(queries):
        scores: dict[str, float] = {}
        for page_id, score in zip(page_ids, similarities[row]):
            scores[str(page_id)] = max(scores.get(str(page_id), -1.0), float(score))
        cache[query] = scores

def semantic_page_ranking(
    query: str,
    pages: list[PageUnit],
    semantic_index: dict[str, Any] | None,
    *,
    query_vector: list[float] | None = None,
) -> list[tuple[float, PageUnit]]:
    if not semantic_index or not pages:
        return []
    if query_vector is None:
        query_vectors = embed_texts([query], timeout=gemini_call_timeout_seconds())
        if len(query_vectors) != 1 or not query_vectors[0]:
            raise RuntimeError("embedding provider returned an invalid query vector")
        query_vector = query_vectors[0]
    if not query_vector:
        return []
    allowed = {page.page_id: page for page in pages}
    cache = semantic_index.setdefault("_query_scores", {})
    scores = cache.get(query)
    if scores is None:
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        scores = {}
        for entry in semantic_index.get("entries") or []:
            page_id = str(entry.get("page_id") or "")
            vector = entry.get("vector")
            if not isinstance(vector, list) or len(vector) != len(query_vector):
                continue
            denominator = query_norm * float(entry.get("norm") or 0)
            score = (
                sum(left * right for left, right in zip(query_vector, vector)) / denominator
                if denominator
                else 0.0
            )
            scores[page_id] = max(scores.get(page_id, -1.0), score)
        cache[query] = scores
    return sorted(
        ((score, allowed[page_id]) for page_id, score in scores.items() if page_id in allowed),
        key=lambda pair: (pair[0], pair[1].text_chars),
        reverse=True,
    )

def dynamic_candidate_window(
    ranked: list[tuple[float, PageUnit]],
    policy: dict[str, int | str],
    *,
    recovery: bool = False,
) -> list[PageUnit]:
    """Use score shape and criterion scope; configured counts are safety ceilings only."""
    if not ranked:
        return []
    profile = str(policy.get("profile") or "standard")
    ceiling = (
        EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND
        if recovery
        else max(int(policy["final_pages"]), int(policy["semantic_pool"]))
    )
    floor = min(len(ranked), max(1, int(policy["final_pages"])))
    if recovery:
        floor = 1
    best = ranked[0][0]
    relative_margin = {"objective": 0.06, "standard": 0.10, "broad": 0.16}.get(profile, 0.10)
    selected: list[PageUnit] = []
    previous = best
    for index, (score, page) in enumerate(ranked):
        if len(selected) >= ceiling:
            break
        meaningful_drop = index >= floor and previous - score >= relative_margin
        too_far_from_best = index >= floor and best - score >= relative_margin * 2
        if meaningful_drop or too_far_from_best:
            break
        selected.append(page)
        previous = score
    return selected or [ranked[0][1]]

def unique_pages(pages: list[PageUnit]) -> list[PageUnit]:
    return list({page.page_id: page for page in pages}.values())

def page_content_key(page: PageUnit) -> str:
    normalized = normalize_for_match(clean_text(page.text))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else page.page_id

def content_unique_pages(pages: list[PageUnit]) -> list[PageUnit]:
    """Keep one stable searchable representative for byte-equivalent content."""
    seen: set[str] = set()
    result: list[PageUnit] = []
    for page in pages:
        key = page_content_key(page)
        if key in seen:
            continue
        seen.add(key)
        result.append(page)
    return result

def interleave_unique_page_lanes(
    lanes: list[list[PageUnit]],
    limit: int,
) -> list[PageUnit]:
    """Merge retrieval lanes fairly so a routing hint cannot erase corpus search."""
    selected: list[PageUnit] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    positions = [0 for _ in lanes]
    while len(selected) < max(0, limit):
        advanced = False
        for lane_index, lane in enumerate(lanes):
            while positions[lane_index] < len(lane):
                page = lane[positions[lane_index]]
                positions[lane_index] += 1
                key = page_content_key(page)
                if page.page_id in seen_ids or key in seen_content:
                    continue
                selected.append(page)
                seen_ids.add(page.page_id)
                seen_content.add(key)
                advanced = True
                break
            if len(selected) >= limit:
                break
        if not advanced:
            break
    return selected
