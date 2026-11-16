"""Source-grounded base-RFP extraction and amendment processing."""

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from ai_provider import is_provider_spend_cap_error
from base_rfp import (
    BaseRfpAmendmentEvent,
    BaseRfpGap,
    BaseRfpUnderstanding,
    apply_base_rfp_amendments,
    create_base_rfp_understanding,
    normalize_base_rfp_partial,
)
from rfp_atoms import RfpAtomBatch, normalize_atom_batch, project_atoms_to_base_rfp

from procurement.domain.catalog import ATOM_TYPES, GROUPABLE_ATOM_TYPES
from procurement.domain.schemas import ExtractedDocument, LayoutTableCell, LogicalDocument, PageUnit
from procurement.domain.text import clean_text, normalize_for_match
from procurement.platform.job_context import JOB_CONTEXT
from procurement.platform.files import write_json_atomic
from procurement.platform.llm import (
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MODEL_DEEP,
    GeminiOutputTruncated,
    llm_json,
)
from procurement.platform.prompts import prompt_text
from procurement.understanding.amendments import (
    base_rfp_amendment_response_schema,
    base_rfp_amendment_source_batches,
    base_rfp_amendment_target_catalog,
    base_rfp_amendment_targets_for_units,
    normalize_base_rfp_amendment_response,
)
from procurement.understanding.layout import coerce_int, layout_blocks_from_text
from procurement.understanding.source_units import base_rfp_source_unit_map
from procurement.understanding.sources import (
    identify_base_rfp_set,
    selected_base_rfp_pages,
    selected_post_base_rfp_pages,
)


logger = logging.getLogger("procurement_evaluator")

BASE_RFP_CHUNK_MAX_UNITS = max(1, int(os.getenv("BASE_RFP_CHUNK_MAX_UNITS", "60")))
BASE_RFP_CHUNK_MAX_SOURCE_CHARS = max(1000, int(os.getenv("BASE_RFP_CHUNK_MAX_SOURCE_CHARS", "24000")))
BASE_RFP_ATOM_MAX_OUTPUT_TOKENS = max(512, int(os.getenv("BASE_RFP_ATOM_MAX_OUTPUT_TOKENS", "4096")))
BASE_RFP_ATOM_OUTPUT_TOKENS_PER_UNIT = max(32, int(os.getenv("BASE_RFP_ATOM_OUTPUT_TOKENS_PER_UNIT", "48")))
BASE_RFP_EXTRACTION_CONCURRENCY = max(1, int(os.getenv("BASE_RFP_EXTRACTION_CONCURRENCY", "3")))
BASE_RFP_EXTRACTION_VERSION = "base-rfp-atoms-v1"
BASE_RFP_AMENDMENT_VERSION = "base-rfp-amendments-v2-claim-first"


def base_rfp_understanding_agent(
    tender_docs: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
    selections: list[dict[str, Any]] | None = None,
    checkpoint_dir: Path | None = None,
) -> BaseRfpUnderstanding:
    selections = selections if selections is not None else identify_base_rfp_set(logical_documents)
    pages = selected_base_rfp_pages(selections, tender_docs)
    chunks = base_rfp_semantic_chunks(pages)
    source_documents = base_rfp_source_documents(tender_docs, pages)
    if not chunks:
        return create_base_rfp_understanding(
            {
                "source_documents": source_documents,
                "gaps": [{"gap_type": "missing_base_rfp", "severity": "critical", "description": "No readable base-RFP pages were available."}],
                "coverage": {"status": "failed", "documents_total": len(source_documents), "pages_total": 0},
                "audit": {"created_at": datetime.now(timezone.utc).isoformat(), "created_by": "base_rfp_understanding_agent"},
            }
        )

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    if checkpoint_dir:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def run_chunk(
        item: tuple[int, tuple[list[PageUnit], dict[str, dict[str, Any]]]],
    ) -> tuple[dict[str, Any] | None, list[str], str | None, bool, dict[str, Any] | None]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        index, (chunk_pages, source_units) = item
        page_ids = list(dict.fromkeys(unit["page_id"] for unit in source_units.values()))
        cache_path: Path | None = None
        if checkpoint_dir:
            cache_path = base_rfp_semantic_cache_path(checkpoint_dir, source_units)
            if cache_path.is_file():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    batch = normalize_atom_batch(cached["atom_batch"], source_units)
                    partial = normalize_base_rfp_partial(project_atoms_to_base_rfp(batch, source_units))
                    return partial, page_ids, None, True, batch.model_dump(mode="json")
                except (KeyError, OSError, ValueError, json.JSONDecodeError):
                    pass
        try:
            partial, batch = base_rfp_chunk_data_resilient(chunk_pages, index + 1, len(chunks), source_units)
            normalized = normalize_base_rfp_partial(partial)
            if cache_path:
                write_json_atomic(cache_path, {"partial": normalized, "atom_batch": batch})
            return normalized, page_ids, None, False, batch
        except Exception as exc:
            if is_provider_spend_cap_error(exc):
                raise
            logger.warning("Base RFP chunk %s failed: %s", index + 1, exc)
            return None, page_ids, str(exc)[:600], False, None

    indexed_chunks = list(enumerate(chunks))
    if BASE_RFP_EXTRACTION_CONCURRENCY == 1 or len(chunks) == 1:
        results = [run_chunk(item) for item in indexed_chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(BASE_RFP_EXTRACTION_CONCURRENCY, len(chunks))) as executor:
            results = [future.result() for future in [executor.submit(run_chunk, item) for item in indexed_chunks]]

    partials = [partial for partial, _page_ids, error, _cache_hit, _batch in results if partial is not None and not error]
    failed = [(page_ids, error) for partial, page_ids, error, _cache_hit, _batch in results if partial is None or error]
    cache_hits = sum(1 for _partial, _page_ids, error, cache_hit, _batch in results if cache_hit and not error)
    atom_batches = [batch for _partial, _page_ids, error, _cache_hit, batch in results if batch and not error]
    JOB_CONTEXT.base_rfp_atoms = atom_batches
    all_source_units = base_rfp_source_unit_map(pages)
    expected_unit_ids = set(all_source_units)
    annotated_unit_ids = {
        unit_id
        for batch in atom_batches
        for atom in batch.get("atoms") or []
        for unit_id in atom.get("source_unit_ids") or []
    }
    page_unit_ids = {
        page.page_id: {unit_id for unit_id, unit in all_source_units.items() if unit["page_id"] == page.page_id}
        for page in pages
    }
    covered_page_ids = {
        page_id
        for page_id, unit_ids in page_unit_ids.items()
        if unit_ids and unit_ids <= annotated_unit_ids
    }
    selected_page_ids = {page.page_id for page in pages}
    covered_pages = [page for page in pages if page.page_id in covered_page_ids]
    covered_document_ids = {
        page.document_id
        for page in covered_pages
        if all(candidate.page_id in covered_page_ids for candidate in pages if candidate.document_id == page.document_id)
    }
    deterministic_partial: dict[str, Any] = {
        "source_documents": source_documents,
        "gaps": [
            {
                "gap_type": "chunk_extraction_failure",
                "severity": "critical",
                "description": f"Base-RFP extraction failed for pages: {', '.join(page_ids)}.",
                "impact": error or "Semantic content from these pages is unavailable.",
                "affected_ids": page_ids,
            }
            for page_ids, error in failed
        ],
        "coverage": {
            "status": "complete" if covered_page_ids == selected_page_ids else ("partial" if covered_page_ids else "failed"),
            "documents_total": len(source_documents),
            "documents_covered": len(covered_document_ids),
            "pages_total": len(pages),
            "pages_covered": len(covered_page_ids),
            "blocks_covered": sum(len(page.blocks) for page in covered_pages),
            "tables_covered": sum(len(page.tables) for page in covered_pages),
            "cells_covered": sum(len(table.cells) for page in covered_pages for table in page.tables),
            "uncovered_page_ids": sorted(selected_page_ids - covered_page_ids),
            "notes": [
                "Coverage is operational: every source unit on a covered page was annotated; it does not claim perfect semantic recall.",
                f"Source units annotated: {len(annotated_unit_ids & expected_unit_ids)}/{len(expected_unit_ids)}.",
            ],
        },
        "audit": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "base_rfp_understanding_agent",
            "chunk_count": len(chunks),
            "notes": [
                "Base RFP only; corrigenda and pre-bid clarifications are reserved for the next versioning layer.",
                f"Semantic chunk checkpoint hits: {cache_hits}.",
            ],
        },
    }
    record = create_base_rfp_understanding([deterministic_partial, *partials])
    return verify_base_rfp_understanding(record, pages)

def effective_rfp_understanding_agent(
    base: BaseRfpUnderstanding,
    tender_docs: list[ExtractedDocument],
    logical_documents: list[LogicalDocument],
    selections: list[dict[str, Any]],
    checkpoint_dir: Path | None = None,
) -> tuple[BaseRfpUnderstanding, list[BaseRfpAmendmentEvent], list[str]]:
    update_pages = selected_post_base_rfp_pages(selections, tender_docs)
    if not update_pages:
        return base, [], []
    page_by_id = {page.page_id: page for page in update_pages}
    update_selections = [selection for selection in selections if selection.get("role") == "post_base_update"]
    catalog = base_rfp_amendment_target_catalog(base)
    events: list[BaseRfpAmendmentEvent] = []
    warnings: list[str] = []
    if checkpoint_dir:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    def process_batch(
        selection: dict[str, Any],
        source_units: dict[str, dict[str, Any]],
        chunk_label: str,
    ) -> tuple[list[BaseRfpAmendmentEvent], list[str]]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        targets = base_rfp_amendment_targets_for_units(catalog, source_units)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version": BASE_RFP_AMENDMENT_VERSION,
                    "model": GEMINI_MODEL_DEEP,
                    "base_record_id": base.record_id,
                    "source_units": source_units,
                    "targets": targets,
                },
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        cache_path = checkpoint_dir / f"{fingerprint}.json" if checkpoint_dir else None
        try:
            if cache_path and cache_path.is_file():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                data = llm_json(
                    prompt_text(
                        "base_rfp_amendments",
                        "You convert an official corrigendum or pre-bid clarification into source-grounded changes to an existing RFP. Return JSON only.",
                    ),
                    {
                        "task": f"extract_base_rfp_amendments_chunk_{chunk_label}",
                        "rules": [
                            "Read every supplied source unit and emit every explicit add, replacement, deletion, restatement, or meaning-changing clarification.",
                            "First extract compact change claims from the update itself. Do not enumerate or guess possible base-RFP targets.",
                            "Return no event for unchanged publication, portal, disclaimer, signature, contact, or submission instructions unless the update explicitly says they replace or add a base-RFP rule.",
                            "A date extension replaces the matching schedule value. A revised criterion, clause, table row, amount, or term replaces its exact base target.",
                            "Put the section, clause, annexure, row label, or field being changed in target_hint. Put the old value or old wording in previous_value_json when the update supplies it; otherwise use an empty string.",
                            "target_path is only a semantic collection hint such as schedule.events, evaluation.criteria, clauses, requirements, or required_documents. Use unresolved when the collection is unclear.",
                            "Do not emit target_candidate_id; target resolution is performed separately against the canonical RFP after extraction.",
                            "For an addition, use an allowed target_path and provide the complete object required by that collection.",
                            "value_json must be valid compact JSON. For a string it must include JSON quotes. For delete use an empty value_json.",
                            "When the selected target current_value is a scalar, value_json must contain the replacement scalar, not an object describing it.",
                            "issued_at and effective_at must be ISO-8601 dates explicitly supported by the update. Do not invent chronology.",
                            "As-per-RFP answers and bidder requests that the buyer did not accept are not changes and must emit no event.",
                            "Document reference numbers and the corrigendum's own issue date are metadata, not changes to the base RFP.",
                            "Emit at most one event for each explicit changed field or clause. Never repeat an event, and never emit more than eight events for one source unit.",
                            "Copy source_unit_ids exactly. Never create source IDs, target IDs, or facts.",
                        ],
                        "logical_document": {
                            "logical_document_id": selection.get("logical_document_id"),
                            "document_type": selection.get("document_type"),
                            "page_refs": selection.get("page_refs") or [],
                        },
                        "source_units": [
                            {"unit_id": unit_id, "text": unit["text"], "section_path": unit.get("section_path") or []}
                            for unit_id, unit in source_units.items()
                        ],
                        "allowed_add_paths": ["clauses", "requirements", "schedule.events", "evaluation.criteria", "required_documents", "annexures", "definitions", "policies.items"],
                    },
                    model=GEMINI_MODEL_DEEP,
                    response_schema=base_rfp_amendment_response_schema(),
                    max_output_tokens=4096,
                    json_retry=False,
                )
                if cache_path:
                    write_json_atomic(cache_path, data)
            return normalize_base_rfp_amendment_response(data, source_units, targets)
        except Exception as exc:
            if "maxOutputTokens" in str(exc) and len(source_units) > 1:
                rows = list(source_units.items())
                middle = len(rows) // 2
                left_events, left_warnings = process_batch(selection, dict(rows[:middle]), f"{chunk_label}a")
                right_events, right_warnings = process_batch(selection, dict(rows[middle:]), f"{chunk_label}b")
                return [*left_events, *right_events], [*left_warnings, *right_warnings]
            return [], [f"Amendment chunk {chunk_label} failed: {str(exc)[:500]}"]

    jobs: list[tuple[dict[str, Any], dict[str, dict[str, Any]], str]] = []
    for selection in update_selections:
        pages = [page_by_id[page_id] for page_id in selection.get("page_refs") or [] if page_id in page_by_id]
        logical_units = base_rfp_source_unit_map(pages)
        for source_units in base_rfp_amendment_source_batches(logical_units):
            jobs.append((selection, source_units, str(len(jobs) + 1)))

    if BASE_RFP_EXTRACTION_CONCURRENCY == 1 or len(jobs) <= 1:
        results = [process_batch(*job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(BASE_RFP_EXTRACTION_CONCURRENCY, len(jobs))) as executor:
            futures = [executor.submit(process_batch, *job) for job in jobs]
            results = [future.result() for future in futures]
    for chunk_events, chunk_warnings in results:
        events.extend(chunk_events)
        warnings.extend(chunk_warnings)

    unique_events: list[BaseRfpAmendmentEvent] = []
    seen_events: set[str] = set()
    for event in events:
        marker = json.dumps(event.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, default=str)
        if marker not in seen_events:
            seen_events.add(marker)
            unique_events.append(event)
    effective = apply_base_rfp_amendments(base, unique_events)
    for warning in warnings:
        if warning not in effective.audit.warnings:
            effective.audit.warnings.append(warning)
    return effective, unique_events, warnings

def base_rfp_chunk_data(
    pages: list[PageUnit],
    chunk_number: int,
    chunk_count: int,
    source_units: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_units = source_units or base_rfp_source_unit_map(pages)
    wire_id_by_unit = {unit_id: f"u{index}" for index, unit_id in enumerate(source_units, start=1)}
    unit_id_by_wire = {wire_id: unit_id for unit_id, wire_id in wire_id_by_unit.items()}
    payload = {
        "task": f"extract_base_rfp_atoms_chunk_{chunk_number}",
        "chunk_number": chunk_number,
        "chunk_count": chunk_count,
        "rules": [
            "This is an exhaustive annotation sweep, not retrieval. Every supplied unit_id must appear in at least one g.s or a.s list.",
            "Use g only for clause, other, definition, or non_semantic units that need no normalized field, cross-label, or extra attribute. Group many same-type unit IDs into one g record.",
            "Every identity, party, procurement, scope, schedule, financial_term, contract_term, policy, evaluation, bid_rule, annexure, required_document, and requirement must use a with enough k, m, v, or x detail to preserve its meaning.",
            "Merge continuation units into one a.s list when they jointly form one buyer name, title, scope, schedule entry, clause, annexure, or other fact.",
            "For requirement, bid_rule, policy, evaluation, and contract_term atoms, use a concise m unless one short k or v fully preserves the actionable meaning.",
            "For identity, party, procurement, scope, schedule, financial_term, annexure, and required_document atoms, use k plus v or explicit x attributes.",
            "Use a for explicit key/value details, cross-labels, or multiple independent atoms from one source unit.",
            "Do not copy, quote, summarize, or paraphrase source text. The application keeps the source wording locally. Use m only to distinguish multiple independent atoms from one source unit.",
            "Use non_semantic for repeated headers, footers, page numbers, signatures without tender meaning, or layout furniture.",
            "Use clause or other for descriptive and procedural tender content so it is not discarded merely because it is not a bid gate.",
            "Use requirement only for an obligation, prohibition, qualification, submission, scored condition, threshold, consequence, or bidder/contractor action.",
            "Set r=true in a whenever the atom imposes an obligation, threshold, submission, applicability decision, scored condition, or rejection consequence but its primary type is not requirement.",
            "Set d=true in a only when the atom names a bidder artifact but its primary type is not required_document.",
            "Use required_document only for a named artifact the bidder must submit, complete, sign, upload, or provide as evidence. A section heading or prose instruction is not a required_document.",
            "For a listed annexure or attachment, emit annexure. If the bidder must submit, complete, accept, or respond to it, also emit a required_document and requirement.",
            "A source unit may support multiple atoms. Never combine independent table rows or obligations into one atom.",
            "Copy sources exactly from supplied unit_id values. Never return page text, page numbers, quotes, or invented IDs.",
            "Omit r, d, k, m, v, and x when absent or false; do not return nulls, empty strings, empty arrays, or guesses.",
            "Use attributes only for explicit detail. Reserved names are title, actor, action, subject, modality, mandatory, condition, exceptions, category, stage, failure_effect, evidence_names, and values. Other names remain tender-specific properties.",
            "Encode an attribute-list value as a compact JSON string. The application hydrates this wire format into the richer internal atom model.",
        ],
        "atom_types": list(ATOM_TYPES),
        "source_units": [
            {
                "unit_id": wire_id_by_unit[unit_id],
                "unit_type": unit.get("unit_type", "text"),
                "text": unit["text"],
                "section_path": unit.get("section_path") or [],
                "table_headers": unit.get("table_headers") or [],
            }
            for unit_id, unit in source_units.items()
        ],
        "json_shape": {
            "g": [{"t": "one atom_types value", "s": ["unit IDs sharing that type"]}],
            "a": [
                {
                    "t": "one atom_types value",
                    "r": "optional true cross-label",
                    "d": "optional true cross-label",
                    "k": "optional short predicate/field name",
                    "m": "optional disambiguation only for multiple atoms from one unit",
                    "v": "optional scalar value",
                    "x": [{"n": "explicit detail name", "v": "scalar or compact JSON string"}],
                    "s": ["exact supplied unit_id"],
                }
            ],
        },
    }
    system = prompt_text(
        "base_rfp_understanding",
        "You annotate immutable RFP source units into compact semantic atoms. Return one JSON object only and never echo source text.",
    )
    data = llm_json(
        system,
        payload,
        model=GEMINI_MODEL_DEEP,
        response_schema=base_rfp_atom_response_schema(),
        max_output_tokens=base_rfp_atom_output_limit(source_units),
        json_retry=False,
    )
    data = expand_compact_atom_sources(data, unit_id_by_wire)
    try:
        batch = normalize_rfp_atom_response(data, source_units, chunk_number, require_all=False)
    except Exception as first_error:
        retry_payload = {
            **payload,
            "task": f"extract_base_rfp_atoms_chunk_{chunk_number}_schema_retry",
            "validation_error": str(first_error)[:2000],
            "missing_unit_ids": missing_atom_unit_ids(data, source_units),
            "repair_rule": "Return a complete atom response matching json_shape. Include every supplied unit_id and do not repeat source text.",
        }
        repaired = llm_json(
            system,
            retry_payload,
            model=GEMINI_MODEL_DEEP,
            response_schema=base_rfp_atom_response_schema(),
            max_output_tokens=base_rfp_atom_output_limit(source_units),
            json_retry=False,
        )
        repaired = expand_compact_atom_sources(repaired, unit_id_by_wire)
        batch = normalize_rfp_atom_response(repaired, source_units, chunk_number)
    else:
        used_unit_ids = {unit_id for atom in batch.atoms for unit_id in atom.source_unit_ids}
        missing_unit_ids = sorted(set(source_units) - used_unit_ids)
        if missing_unit_ids:
            missing_units = {unit_id: source_units[unit_id] for unit_id in missing_unit_ids}
            repair_payload = {
                **payload,
                "task": f"extract_base_rfp_atoms_chunk_{chunk_number}_missing_units",
                "source_units": [
                    {
                        "unit_id": unit_id,
                        "unit_type": unit.get("unit_type", "text"),
                        "text": unit["text"],
                        "section_path": unit.get("section_path") or [],
                        "table_headers": unit.get("table_headers") or [],
                    }
                    for unit_id, unit in missing_units.items()
                ],
                "repair_rule": "Annotate only these previously missed source units. Include every supplied unit_id exactly.",
            }
            repaired = llm_json(
                system,
                repair_payload,
                model=GEMINI_MODEL_DEEP,
                response_schema=base_rfp_atom_response_schema(),
                max_output_tokens=base_rfp_atom_output_limit(missing_units),
                json_retry=False,
            )
            repaired = expand_compact_atom_sources(repaired, {unit_id: unit_id for unit_id in missing_units})
            repair_batch = normalize_rfp_atom_response(
                repaired,
                missing_units,
                chunk_number + 1_000_000,
            )
            batch = normalize_atom_batch(
                {"atoms": [atom.model_dump(mode="json") for atom in [*batch.atoms, *repair_batch.atoms]]},
                source_units,
            )
    partial = project_atoms_to_base_rfp(batch, source_units)
    return partial, batch.model_dump(mode="json")

def split_semantic_source_text(text: str, max_chars: int = 700) -> list[str]:
    marked = re.split(
        r"\s+(?=(?:[ivxlcdm]{1,10}|[a-z])\.\s+)|\s*(?=[\u2022\uf0b7]\s*)",
        clean_text(text),
        flags=re.IGNORECASE,
    )
    fragments: list[str] = []
    for part in marked:
        part = part.strip()
        while len(part) > max_chars:
            split_at = part.rfind(" ", 0, max_chars + 1)
            if split_at < max_chars // 2:
                split_at = max_chars
            fragments.append(part[:split_at].strip())
            part = part[split_at:].strip()
        if part:
            fragments.append(part)
    return fragments

def collapse_split_atom_batch(
    batch: dict[str, Any],
    original_unit_id: str,
    source_units: dict[str, dict[str, Any]],
) -> RfpAtomBatch:
    atoms = []
    for raw in batch.get("atoms") or []:
        atom = dict(raw)
        atom["source_unit_ids"] = [original_unit_id]
        atoms.append(atom)
    return normalize_atom_batch({"atoms": atoms}, source_units)

def base_rfp_chunk_data_resilient(
    pages: list[PageUnit],
    chunk_number: int,
    chunk_count: int,
    source_units: dict[str, dict[str, Any]],
    split_depth: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return base_rfp_chunk_data(pages, chunk_number, chunk_count, source_units)
    except (GeminiOutputTruncated, json.JSONDecodeError, ValueError) as exc:
        if split_depth >= 10:
            raise
        if len(source_units) == 1:
            original_unit_id, original_unit = next(iter(source_units.items()))
            fragments = split_semantic_source_text(original_unit["text"])
            if len(fragments) < 2:
                raise
            split_units = {
                f"{original_unit_id}::part-{index}": {**original_unit, "text": fragment}
                for index, fragment in enumerate(fragments, start=1)
            }
            logger.warning(
                "Splitting oversized Base RFP source unit %s into %s local fragments after: %s",
                original_unit_id,
                len(split_units),
                exc,
            )
            _partial, split_batch = base_rfp_chunk_data_resilient(
                pages,
                chunk_number * 10 + 1,
                chunk_count,
                split_units,
                split_depth + 1,
            )
            batch = collapse_split_atom_batch(split_batch, original_unit_id, source_units)
            partial = project_atoms_to_base_rfp(batch, source_units)
            return partial, batch.model_dump(mode="json")
        logger.warning(
            "Splitting Base RFP chunk %s with %s units after compact-response failure: %s",
            chunk_number,
            len(source_units),
            exc,
        )
        items = list(source_units.items())
        midpoint = len(items) // 2
        child_batches: list[dict[str, Any]] = []
        for branch, child_items in enumerate((items[:midpoint], items[midpoint:]), start=1):
            child_units = dict(child_items)
            child_page_ids = {unit["page_id"] for unit in child_units.values()}
            child_pages = [page for page in pages if page.page_id in child_page_ids]
            _partial, child_batch = base_rfp_chunk_data_resilient(
                child_pages,
                chunk_number * 10 + branch,
                chunk_count,
                child_units,
                split_depth + 1,
            )
            child_batches.append(child_batch)
        batch = normalize_atom_batch(
            {
                "atoms": [
                    atom
                    for child_batch in child_batches
                    for atom in child_batch.get("atoms") or []
                ]
            },
            source_units,
        )
        partial = project_atoms_to_base_rfp(batch, source_units)
        return partial, batch.model_dump(mode="json")

def base_rfp_atom_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "g": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "t": {"type": "string", "enum": list(GROUPABLE_ATOM_TYPES)},
                        "s": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["t", "s"],
                },
            },
            "a": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "t": {"type": "string", "enum": list(ATOM_TYPES)},
                        "r": {"type": "boolean"},
                        "d": {"type": "boolean"},
                        "k": {"type": "string"},
                        "m": {"type": "string"},
                        "v": {"type": "string"},
                        "x": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"n": {"type": "string"}, "v": {"type": "string"}},
                                "required": ["n", "v"],
                            },
                        },
                        "s": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["t", "s"],
                },
            },
        },
        "required": ["g", "a"],
    }

def base_rfp_semantic_chunks(
    pages: list[PageUnit],
) -> list[tuple[list[PageUnit], dict[str, dict[str, Any]]]]:
    """Bound semantic work by source units and characters, not page count."""
    units = base_rfp_source_unit_map(pages)
    pages_by_id = {page.page_id: page for page in pages}
    chunks: list[tuple[list[PageUnit], dict[str, dict[str, Any]]]] = []
    current: dict[str, dict[str, Any]] = {}
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        page_ids = list(dict.fromkeys(unit["page_id"] for unit in current.values()))
        chunks.append(([pages_by_id[page_id] for page_id in page_ids], current))
        current = {}
        current_chars = 0

    for unit_id, unit in units.items():
        unit_chars = len(unit["text"])
        if current and (
            len(current) >= BASE_RFP_CHUNK_MAX_UNITS
            or current_chars + unit_chars > BASE_RFP_CHUNK_MAX_SOURCE_CHARS
        ):
            flush()
        current[unit_id] = unit
        current_chars += unit_chars
    flush()
    return chunks

def base_rfp_atom_output_limit(source_units: int | dict[str, dict[str, Any]]) -> int:
    source_unit_count = source_units if isinstance(source_units, int) else len(source_units)
    source_characters = 0 if isinstance(source_units, int) else sum(len(unit["text"]) for unit in source_units.values())
    return min(
        GEMINI_MAX_OUTPUT_TOKENS,
        BASE_RFP_ATOM_MAX_OUTPUT_TOKENS,
        max(512, source_unit_count * BASE_RFP_ATOM_OUTPUT_TOKENS_PER_UNIT, source_characters // 8),
    )

def base_rfp_semantic_cache_path(
    checkpoint_dir: Path,
    source_units: dict[str, dict[str, Any]],
) -> Path:
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": BASE_RFP_EXTRACTION_VERSION,
                "model": GEMINI_MODEL_DEEP,
                "source_units": source_units,
            },
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return checkpoint_dir / f"{fingerprint}.json"

def compact_atom_attribute_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return stripped

def expand_compact_atom_sources(data: Any, unit_id_by_wire: dict[str, str]) -> Any:
    if not isinstance(data, dict):
        return data
    if isinstance(data.get("g"), list) or isinstance(data.get("a"), list):
        atoms: list[dict[str, Any]] = []
        for group in data.get("g") or []:
            if not isinstance(group, dict):
                continue
            for source in group.get("s") or []:
                atoms.append({"type": group.get("t"), "sources": [source]})
        aliases = {
            "t": "type",
            "r": "requirement",
            "d": "bidder_document",
            "k": "key",
            "m": "meaning",
            "v": "value",
            "s": "sources",
        }
        for raw in data.get("a") or []:
            if not isinstance(raw, dict):
                continue
            atom = {target: raw[source] for source, target in aliases.items() if source in raw}
            attributes = [
                {"name": item.get("n"), "value": item.get("v")}
                for item in raw.get("x") or []
                if isinstance(item, dict)
            ]
            if attributes:
                atom["attributes"] = attributes
            atoms.append(atom)
        data = {"atoms": atoms}
    if not isinstance(data.get("atoms"), list):
        return data
    for atom in data["atoms"]:
        if not isinstance(atom, dict):
            continue
        for field in ("sources", "source_unit_ids"):
            if isinstance(atom.get(field), list):
                atom[field] = [unit_id_by_wire.get(str(unit_id), str(unit_id)) for unit_id in atom[field]]
    return data

def hydrate_compact_atom(raw: dict[str, Any], index: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "atom_id": str(index),
        "atom_type": raw.get("type"),
        "normalized_text": str(raw.get("meaning") or "").strip(),
        "source_unit_ids": raw.get("sources") or [],
    }
    for field in ("key", "value"):
        value = raw.get(field)
        if value not in (None, ""):
            row[field] = value

    properties: dict[str, Any] = {}
    scalar_fields = {
        "title",
        "actor",
        "action",
        "subject",
        "modality",
        "condition",
        "category",
        "stage",
        "failure_effect",
    }
    list_fields = {"values", "exceptions", "evidence_names"}
    for attribute in raw.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = str(attribute.get("name") or "").strip()
        value = compact_atom_attribute_value(attribute.get("value"))
        if not name or value in (None, "", []):
            continue
        if name in scalar_fields:
            row[name] = str(value)
        elif name == "mandatory":
            if isinstance(value, bool):
                row[name] = value
            elif str(value).lower() in {"true", "false"}:
                row[name] = str(value).lower() == "true"
        elif name in list_fields:
            row[name] = value if isinstance(value, list) else [str(value)]
        elif name in properties:
            current = properties[name]
            properties[name] = current + [value] if isinstance(current, list) else [current, value]
        else:
            properties[name] = value
    if isinstance(raw.get("requirement"), bool):
        properties["is_requirement"] = raw["requirement"]
    if isinstance(raw.get("bidder_document"), bool):
        properties["is_required_document"] = raw["bidder_document"]
    row["properties"] = properties
    return row

def normalize_rfp_atom_response(
    data: Any,
    source_units: dict[str, dict[str, Any]],
    chunk_number: int,
    *,
    require_all: bool = True,
) -> RfpAtomBatch:
    if not isinstance(data, dict) or not isinstance(data.get("atoms"), list):
        raise ValueError("Atom response must contain an atoms array")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(data["atoms"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Atom {index} is not an object")
        row = hydrate_compact_atom(raw, index) if "type" in raw else dict(raw)
        if not row.get("normalized_text"):
            row["normalized_text"] = str(row.get("value") or row.get("title") or row.get("key") or "").strip()
        if not row.get("normalized_text"):
            row["normalized_text"] = " ".join(
                source_units[unit_id]["text"]
                for unit_id in row.get("source_unit_ids") or []
                if unit_id in source_units
            )
        original_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(row.get("atom_id") or index)).strip("-") or str(index)
        row["atom_id"] = f"chunk-{chunk_number}-{index}-{original_id}"
        properties = row.get("properties")
        if isinstance(properties, list):
            normalized_properties: dict[str, Any] = {}
            for item in properties:
                if not isinstance(item, dict) or not str(item.get("key") or "").strip():
                    continue
                key = str(item["key"]).strip()
                value = item.get("value")
                if key in normalized_properties:
                    current = normalized_properties[key]
                    normalized_properties[key] = current + [value] if isinstance(current, list) else [current, value]
                else:
                    normalized_properties[key] = value
            row["properties"] = normalized_properties
        elif properties is None:
            row["properties"] = {}
        rows.append(row)
    batch = normalize_atom_batch({"atoms": rows}, source_units)
    used = {unit_id for atom in batch.atoms for unit_id in atom.source_unit_ids}
    missing = sorted(set(source_units) - used)
    if require_all and missing:
        raise ValueError(f"Unannotated source_unit_ids: {', '.join(missing[:100])}")
    return batch

def missing_atom_unit_ids(data: Any, source_units: dict[str, dict[str, Any]]) -> list[str]:
    used = {
        str(unit_id)
        for atom in (data.get("atoms") if isinstance(data, dict) and isinstance(data.get("atoms"), list) else [])
        if isinstance(atom, dict)
        for unit_id in (atom.get("sources") or atom.get("source_unit_ids") or [])
    }
    return sorted(set(source_units) - used)

def base_rfp_source_documents(tender_docs: list[ExtractedDocument], pages: list[PageUnit]) -> list[dict[str, Any]]:
    selected_by_document: dict[str, list[PageUnit]] = {}
    for page in pages:
        selected_by_document.setdefault(page.document_id, []).append(page)
    rows = []
    for document in tender_docs:
        selected = selected_by_document.get(document.record.document_id)
        if not selected:
            continue
        checksum = ""
        if document.source_path and document.source_path.is_file():
            try:
                with document.source_path.open("rb") as handle:
                    checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError:
                checksum = ""
        classification = document.record.classification
        rows.append(
            {
                "document_id": document.record.document_id,
                "file_name": document.record.file_name,
                "title": classification.title if classification and classification.title else document.record.file_name,
                "document_type": classification.document_type if classification else "unknown",
                "checksum_sha256": checksum,
                "page_count": document.record.page_count,
                "status": document.record.extraction_status,
                "notes": ["Selected base page IDs: " + ", ".join(page.page_id for page in selected)],
            }
        )
    return rows

def verify_base_rfp_understanding(record: BaseRfpUnderstanding, pages: list[PageUnit]) -> BaseRfpUnderstanding:
    page_by_id = {page.page_id: page for page in pages}
    invalid: list[tuple[str, str]] = []
    for row in record.clauses:
        if not row.source_refs or not any(base_rfp_source_ref_is_valid(ref, page_by_id) for ref in row.source_refs):
            row.status = "unresolved"
    for row in record.requirements:
        if not row.source_refs or not any(base_rfp_source_ref_is_valid(ref, page_by_id) for ref in row.source_refs):
            row.status = "unresolved"

    semantic = record.model_dump(mode="json", exclude={"source_documents", "gaps", "coverage", "audit"})

    def inspect(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, path + (str(index),))
            return
        if not isinstance(value, dict):
            return
        source_refs = value.get("source_refs")
        identifier = next(
            (str(item) for key, item in value.items() if key.endswith("_id") and item),
            "",
        )
        is_semantic_record = bool(identifier or value.get("key"))
        if is_semantic_record and source_refs is not None:
            label = identifier or ".".join(path)
            if not source_refs:
                invalid.append((label, "No source reference."))
            elif not any(base_rfp_source_ref_is_valid(ref, page_by_id) for ref in source_refs):
                invalid.append((label, "No source reference resolved to an exact source quote."))
        for key, item in value.items():
            if key != "source_refs":
                inspect(item, path + (key,))

    inspect(semantic)
    invalid = list(dict.fromkeys(invalid))
    for row_id, reason in invalid:
        record.gaps.append(
            BaseRfpGap(
                gap_type="invalid_provenance",
                severity="critical",
                description=f"{row_id}: {reason}",
                affected_ids=[row_id],
                impact="The record is retained but cannot be treated as audit-ready.",
            )
        )
    if invalid:
        record.coverage.status = "partial" if record.coverage.pages_covered else "failed"
        record.coverage.notes.append(f"{len(invalid)} clause/requirement records failed provenance verification.")
    return create_base_rfp_understanding(record.model_dump(mode="json"))

def base_rfp_source_ref_is_valid(ref: Any, page_by_id: dict[str, PageUnit]) -> bool:
    def ref_value(name: str) -> Any:
        return ref.get(name) if isinstance(ref, dict) else getattr(ref, name, "")

    page = page_by_id.get(str(ref_value("page_id") or ""))
    if not page or not ref_value("quote"):
        return False
    referenced_page = ref_value("page") or ref_value("page_number")
    if referenced_page and coerce_int(referenced_page) != page.page_number:
        return False
    source_text = page.text
    block_id = str(ref_value("block") or ref_value("block_id") or "")
    table_id = str(ref_value("table") or ref_value("table_id") or "")
    cell_id = str(ref_value("cell") or ref_value("cell_id") or "")
    if cell_id:
        source_text = next((cell.text for table in page.tables for cell in table.cells if cell.cell_id == cell_id), "")
    elif table_id:
        table = next((table for table in page.tables if table.table_id == table_id), None)
        if table:
            rows: dict[int, list[LayoutTableCell]] = {}
            for cell in table.cells:
                rows.setdefault(cell.row_index, []).append(cell)
            row_text = "\n".join(
                " | ".join(
                    cell.text
                    for cell in sorted(cells, key=lambda item: item.column_index)
                    if cell.text
                )
                for _row_index, cells in sorted(rows.items())
            )
            source_text = f"{row_text}\n{table.markdown}"
        else:
            source_text = ""
    elif block_id:
        blocks = page.blocks or layout_blocks_from_text(page.layout_text or page.text, page.page_id)
        block_index = next((index for index, block in enumerate(blocks) if block.block_id == block_id), -1)
        source_text = "\n".join(block.text for block in blocks[block_index : block_index + 12]) if block_index >= 0 else ""
    quote = normalize_for_match(str(ref_value("quote") or ""))
    source = normalize_for_match(source_text)
    return bool(quote and source and quote in source)
