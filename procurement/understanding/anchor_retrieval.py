"""Anchor-page selection and visual fact extraction for tender understanding."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from procurement.platform.files import write_json_atomic
from procurement.understanding.field_compiler import RfpUnderstandingSchema

logger = logging.getLogger("procurement.understanding")
_ANCHOR_SECTION_MARKERS = {
    "bid qualification criteria": 60,
    "pre qualification criteria": 55,
    "eligibility criteria": 50,
    "technical audit checklist": 60,
    "qualification parameters": 45,
    "documents required": 40,
    "documents to be submitted": 40,
    "to be submitted with technical bid": 50,
    "evaluation criteria": 45,
    "bid details": 35,
    "tender details": 35,
    "invitation to bid": 30,
    "additional terms and conditions": 25,
    "special purchase conditions": 25,
    "scope of work": 25,
    "buyer added bid specific": 25,
}
_ANCHOR_MODAL_TERMS = (
    "shall",
    "must",
    "mandatory",
    "required",
    "qualify",
    "eligible",
    "submit",
    "upload",
    "evidence",
    "proof",
    "certificate",
    "declaration",
    "undertaking",
)
def _anchor_normalize(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _anchor_page_score(page: Any, schema: RfpUnderstandingSchema) -> tuple[int, list[str], bool]:
    text = _anchor_normalize(getattr(page, "text", ""))
    if not text:
        return 0, [], False
    wrapped = f" {text} "
    blocks = getattr(page, "blocks", None) or []
    heading_text = " ".join(
        _anchor_normalize(getattr(block, "text", ""))
        for index, block in enumerate(blocks)
        if str(getattr(block, "block_type", "")) in {"heading", "header"}
        or index < 5
    )
    heading_wrapped = f" {heading_text} "
    marker_score = 0
    heading_marker = False
    for phrase, weight in _ANCHOR_SECTION_MARKERS.items():
        if f" {phrase} " in heading_wrapped:
            marker_score += weight
            heading_marker = True
        elif f" {phrase} " in wrapped:
            marker_score += max(8, weight // 3)

    generic_phrases = {
        "bidder",
        "buyer",
        "evaluation",
        "location",
        "subject",
        "tender summary",
    }
    covered_fields: list[str] = []
    for section in schema.sections:
        for field in section.fields:
            guidance = field.retrieval_guidance
            phrases = {
                _anchor_normalize(value)
                for value in (
                    field.label,
                    *guidance.aliases,
                    *guidance.keywords,
                    *guidance.anchor_phrases,
                    *guidance.binding_phrases,
                )
            }
            if any(
                phrase
                and len(phrase) >= 3
                and phrase not in generic_phrases
                and f" {phrase} " in wrapped
                for phrase in phrases
            ):
                covered_fields.append(field.key)

    modal_score = min(
        30,
        sum(wrapped.count(f" {term} ") for term in _ANCHOR_MODAL_TERMS),
    )
    tables = getattr(page, "tables", None) or []
    cell_count = sum(len(getattr(table, "cells", None) or []) for table in tables)
    table_score = min(30, len(tables) * 5 + cell_count // 4)
    return (
        len(covered_fields) * 8 + marker_score + modal_score + table_score,
        covered_fields,
        heading_marker,
    )


def _select_anchor_page_clusters(
    documents: list[Any],
    schema: RfpUnderstandingSchema,
    *,
    max_clusters: int,
    pages_per_cluster: int,
) -> list[list[Any]]:
    if max_clusters <= 0 or pages_per_cluster <= 0:
        return []
    pages_by_document: dict[str, list[Any]] = {}
    candidates: list[tuple[int, int, bool, Any]] = []
    for document in documents:
        pages = sorted(document.pages, key=lambda page: int(page.page_number))
        if not pages:
            continue
        pages_by_document[str(pages[0].document_id)] = pages
        for page in pages:
            score, covered_fields, heading_marker = _anchor_page_score(page, schema)
            candidates.append((score, len(covered_fields), heading_marker, page))

    minimum_score = max(1, int(os.getenv("RFP_ANCHOR_MIN_SCORE", "45")))
    selected_page_ids: set[str] = set()
    clusters: list[list[Any]] = []
    for score, _coverage, heading_marker, page in sorted(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
        reverse=True,
    ):
        if len(clusters) >= max_clusters or score < minimum_score:
            break
        if page.page_id in selected_page_ids:
            continue
        document_pages = pages_by_document[str(page.document_id)]
        page_index = document_pages.index(page)
        if heading_marker:
            start = page_index
        elif page_index <= 2:
            start = 0
        else:
            start = page_index - 1
        cluster = document_pages[start : start + pages_per_cluster]
        fresh = [item for item in cluster if item.page_id not in selected_page_ids]
        if len(fresh) < max(1, len(cluster) // 2):
            continue
        clusters.append(cluster)
        selected_page_ids.update(item.page_id for item in cluster)
    return clusters


def _anchor_response_schema(field_keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "tender_fact",
                                "qualification_rule",
                                "proof_requirement",
                                "condition_or_exception",
                                "evaluation_rule",
                            ],
                        },
                        "statement": {"type": "string"},
                        "quote": {"type": "string"},
                        "page_id": {"type": "string"},
                        "field_keys": {
                            "type": "array",
                            "items": {"type": "string", "enum": field_keys},
                        },
                    },
                    "required": ["kind", "statement", "quote", "page_id", "field_keys"],
                },
            },
            "continuation_incomplete": {"type": "boolean"},
        },
        "required": ["facts", "continuation_incomplete"],
    }


def _anchor_fact_units(
    response: dict[str, Any],
    cluster: list[Any],
    base_units: dict[str, dict[str, Any]],
    field_labels: dict[str, str],
) -> dict[str, dict[str, Any]]:
    pages = {str(page.page_id): page for page in cluster}
    page_metadata = {
        page_id: next(
            (unit for unit in base_units.values() if str(unit.get("page_id") or "") == page_id),
            {},
        )
        for page_id in pages
    }
    result: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for fact in response.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        page_id = str(fact.get("page_id") or "")
        statement = " ".join(str(fact.get("statement") or "").split())[:1600]
        quote = " ".join(str(fact.get("quote") or "").split())[:800]
        if page_id not in pages or not statement or not quote:
            continue
        identity = (page_id, _anchor_normalize(statement))
        if identity in seen:
            continue
        seen.add(identity)
        field_keys = [
            str(key)
            for key in fact.get("field_keys") or []
            if str(key) in field_labels
        ]
        labels = [field_labels[key] for key in field_keys]
        page = pages[page_id]
        metadata = page_metadata[page_id]
        digest = hashlib.sha256(f"{page_id}|{statement}".encode("utf-8")).hexdigest()[:16]
        source_id = f"anchor-{digest}"
        result[source_id] = {
            **{
                key: value
                for key, value in metadata.items()
                if key not in {"text", "block", "table", "unit_type"}
            },
            "unit_type": "anchor_fact",
            "text": " | ".join(
                part
                for part in (
                    f"Relevant fields: {', '.join(labels)}" if labels else "",
                    f"{str(fact.get('kind') or 'tender_fact').replace('_', ' ').title()}: {statement}",
                    f"Source quote: {quote}",
                )
                if part
            ),
            "document": str(page.document_id),
            "page": int(page.page_number),
            "page_id": page_id,
            "anchor_field_keys": field_keys,
            "anchor_kind": str(fact.get("kind") or "tender_fact"),
            "anchor_fact": True,
            "visual_evidence": True,
        }
    return result


def _build_anchor_page_units(
    operation_id: str,
    documents: list[Any],
    schema: RfpUnderstandingSchema,
    base_units: dict[str, dict[str, Any]],
    *,
    call: Any,
    image_loader: Any,
    checkpoint_root: Path,
    model_tag: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    max_clusters = max(0, int(os.getenv("RFP_ANCHOR_MAX_CLUSTERS", "4")))
    pages_per_cluster = max(1, int(os.getenv("RFP_ANCHOR_PAGES_PER_CLUSTER", "4")))
    clusters = _select_anchor_page_clusters(
        documents,
        schema,
        max_clusters=max_clusters,
        pages_per_cluster=pages_per_cluster,
    )
    field_labels = {
        field.key: field.label
        for section in schema.sections
        for field in section.fields
    }
    audit: dict[str, Any] = {
        "enabled": True,
        "clusters": [],
        "fact_count": 0,
        "degraded": False,
    }
    if not clusters:
        return {}, audit

    checkpoint_dir = checkpoint_root / "anchors"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    response_schema = _anchor_response_schema(list(field_labels))

    def understand_cluster(index_and_cluster: tuple[int, list[Any]]) -> tuple[int, list[Any], dict[str, Any], bool]:
        index, cluster = index_and_cluster
        page_ids = [str(page.page_id) for page in cluster]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "prompt_version": 2,
                    "schema_id": schema.schema_id,
                    "model": model_tag,
                    "pages": [
                        [page.page_id, hashlib.sha256(str(page.text or "").encode("utf-8")).hexdigest()]
                        for page in cluster
                    ],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        checkpoint = checkpoint_dir / f"cluster-{index + 1}-{fingerprint}.json"
        if checkpoint.exists():
            try:
                return index, cluster, json.loads(checkpoint.read_text(encoding="utf-8")), True
            except (OSError, ValueError):
                pass
        media_parts: list[dict[str, Any]] = []
        for page_id in page_ids:
            image = image_loader(page_id)
            if not image:
                continue
            media_parts.extend(
                [
                    {"text": f"ANCHOR_PAGE_IMAGE page_id={page_id}"},
                    {
                        "inline_data": {
                            "mime_type": image["mime_type"],
                            "data": image["data"],
                        }
                    },
                ]
            )
        response = call(
            system=(
                "Read the supplied tender pages as one continuous, visually structured section. "
                "Extract all explicit tender facts and bidder-evaluation rules once so later field "
                "resolvers can reuse them. Preserve table headers, row relationships, AND/OR logic, "
                "per-location values, totals, applicability, exceptions, bid-stage proof, and whether "
                "a requirement is pre-award or post-award. Distinguish the overall procurement title "
                "from individual BOQ line items. Preserve apparently duplicated or contradictory table "
                "rows exactly and flag the contradiction in the statement. Never infer an unstated rule."
            ),
            payload={
                "task": "understand_tender_anchor_pages",
                "schema_id": schema.schema_id,
                "pages": [
                    {
                        "page_id": str(page.page_id),
                        "document_id": str(page.document_id),
                        "page_number": int(page.page_number),
                        "extracted_text": str(page.text or "")[:7000],
                    }
                    for page in cluster
                ],
                "field_catalog": [
                    {"key": key, "label": label}
                    for key, label in field_labels.items()
                ],
                "rules": [
                    "Treat headings, introductory paragraphs, tables, footnotes, and continuation text as one logical block.",
                    "Create separate facts when one row contains multiple thresholds, locations, proofs, conditions, or alternatives.",
                    "Treat a BOQ title or name of work as the tender subject; do not promote one line-item description to the overall procurement category.",
                    "If a label appears in multiple table rows with different values, preserve each row and explicitly state that the source is ambiguous.",
                    "Use only a supplied page_id and include a concise source quote for every fact.",
                    "Map each fact to every relevant field key; an empty field_keys list is allowed for a real evaluation rule outside the catalog.",
                    "Do not convert contract-execution obligations into bidder qualification gates.",
                ],
            },
            response_schema=response_schema,
            max_output_tokens=max(1024, int(os.getenv("RFP_ANCHOR_MAX_OUTPUT_TOKENS", "8192"))),
            media_parts=media_parts,
        )
        write_json_atomic(checkpoint, response, indent=None)
        return index, cluster, response, False

    anchor_units: dict[str, dict[str, Any]] = {}
    concurrency = min(
        len(clusters),
        max(1, int(os.getenv("RFP_ANCHOR_CONCURRENCY", "2"))),
    )
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(understand_cluster, item): item[0]
            for item in enumerate(clusters)
        }
        for future in as_completed(futures):
            index = futures[future]
            cluster = clusters[index]
            try:
                _index, _cluster, response, cache_hit = future.result()
                facts = _anchor_fact_units(response, cluster, base_units, field_labels)
                anchor_units.update(facts)
                audit["clusters"].append(
                    {
                        "page_ids": [str(page.page_id) for page in cluster],
                        "fact_count": len(facts),
                        "cache_hit": cache_hit,
                        "continuation_incomplete": bool(response.get("continuation_incomplete")),
                    }
                )
            except Exception as exc:
                audit["degraded"] = True
                audit["clusters"].append(
                    {
                        "page_ids": [str(page.page_id) for page in cluster],
                        "fact_count": 0,
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                    }
                )
                logger.warning(
                    "ANCHOR job_id=%s stage=cluster_degraded pages=%s error_type=%s",
                    operation_id,
                    ",".join(str(page.page_id) for page in cluster),
                    type(exc).__name__,
                )
    audit["clusters"].sort(key=lambda item: item["page_ids"])
    audit["fact_count"] = len(anchor_units)
    return anchor_units, audit


def _selected_pages(documents: list[Any], selections: list[dict[str, Any]]) -> list[Any]:
    # Every uploaded tender page remains searchable. Document roles influence
    # authority and amendment precedence; they must not hide relevant evidence.
    return [page for document in documents for page in document.pages]


def _enrich_source_roles(
    units: dict[str, dict[str, Any]],
    selections: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    page_metadata: dict[str, dict[str, Any]] = {}
    for document_order, selection in enumerate(selections, start=1):
        metadata = {
            "source_role": str(selection.get("role") or "unknown"),
            "logical_document_id": str(selection.get("logical_document_id") or ""),
            "document_type": str(selection.get("document_type") or "unknown"),
            "revision_sequence": selection.get("revision_sequence"),
            "revision_number": selection.get("revision_number"),
            "revision_date": selection.get("revision_date"),
            "revision_order_warning": selection.get("revision_order_warning"),
            "authority_tier": selection.get("authority_tier"),
            "document_order": document_order,
            "source_file": str(next(iter(selection.get("source_files") or []), "")),
        }
        for page_id in selection.get("page_refs") or []:
            page_metadata[str(page_id)] = metadata
    return {
        source_id: {
            **unit,
            **{
                key: value
                for key, value in page_metadata.get(str(unit.get("page_id") or ""), {}).items()
                if value not in {None, ""}
            },
            "source_role": page_metadata.get(
                str(unit.get("page_id") or ""), {}
            ).get("source_role", "base_rfp"),
        }
        for source_id, unit in units.items()
    }
