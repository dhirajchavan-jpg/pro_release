"""Stable compatibility facade for tender understanding services."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ai_provider import AIProviderFailure, embed_texts, rerank_texts
from procurement.evaluation.evidence_pipeline import page_source_index
from procurement.platform import llm as llm_runtime
from procurement.platform.job_context import gemini_call_timeout_seconds
from procurement.understanding.anchor_retrieval import (
    _anchor_fact_units,
    _anchor_normalize,
    _anchor_page_score,
    _anchor_response_schema,
    _build_anchor_page_units,
    _enrich_source_roles,
    _select_anchor_page_clusters,
    _selected_pages,
)
from procurement.understanding.field_compiler import (
    RfpUnderstandingSchema,
    compile_field_understanding,
)
from procurement.understanding.rendering import render_pdf_page_png_b64
from procurement.understanding.requirement_evidence import (
    _ATOMIC_DOCUMENT_TYPES,
    _ATOMIC_REQUIREMENT_ORDER,
    _COMMERCIAL_CONTEXT_FIELDS,
    _COMPOSITE_REQUIREMENT_FIELDS,
    _CONDITIONAL_POLICY_FIELDS,
    _EVIDENCE_MATCH_STOPWORDS,
    _EVIDENCE_TOKEN_ALIASES,
    _FIELD_EVIDENCE_SPECS,
    _SHARED_CALL_BUDGET_NOTE,
    _atomic_requirement_values,
    _atomic_value_has_source_table_ambiguity,
    _context_for_atomic_value,
    _criterion_suffix,
    _default_evidence_for_atomic_value,
    _evidence_for_atomic_value,
    _evidence_groups,
    _evidence_match_tokens,
    _pqr_named_proofs,
    _proof_document_type,
    _requirement_semantic_key,
    _requirement_semantics,
    _requirement_values,
    _reviewed_document_type,
    _source_references_for_atomic_value,
    _synchronize_evidence_groups,
    _named_document_spec,
    _shared_call_budget_exhausted,
    evidence_for_reviewed_atomic_field,
    evidence_groups_for_reviewed_atomic_field,
    evidence_requirements_for_field,
    requirement_semantics_for_reviewed_atomic_field,
)
from procurement.understanding.source_units import base_rfp_source_unit_map
from procurement.understanding.tender_field_projection import (
    _EXPLICIT_CONTROL_PATTERNS,
    _GEM_BID_RE,
    _GENERIC_IDENTITY_VALUES,
    _NO_EXEMPTION_PATTERNS,
    _REQUIREMENT_FIELDS,
    _SOURCE_REFERENCE_METADATA,
    _attach_matrix_provenance,
    _canonical_source_reference,
    _canonical_source_references,
    _canonical_source_units,
    _canonicalize_field_evidence,
    _explicit_control_matches,
    _generic_identity_value,
    _identity_candidate,
    _issuer_from_source_units,
    _project_canonical_gem_identity,
    _project_deterministic_tender_controls,
    _project_resolved_tender_summary,
    _project_source_table_ambiguities,
    _provenance_value,
    _refresh_understanding_status,
    _restore_serialized_collection_values,
    _source_unit_for_reference,
    field_entries,
    profile_from_fields,
    requirement_matrix_from_fields,
    summary_from_resolved_fields,
)
from procurement.understanding.tender_understanding import _run_field_understanding

logger = logging.getLogger("procurement.understanding")


def run_field_understanding(
    operation_id: str,
    documents: list[Any],
    logical_documents: list[Any],
    selections: list[dict[str, Any]],
    field_schema: dict[str, Any],
    checkpoint_root: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Resolve configured tender fields while preserving legacy injection seams."""
    return _run_field_understanding(
        operation_id,
        documents,
        logical_documents,
        selections,
        field_schema,
        checkpoint_root,
        source_unit_map=base_rfp_source_unit_map,
        source_index=page_source_index,
        embed=embed_texts,
        rerank=rerank_texts,
        render_page=render_pdf_page_png_b64,
        llm=llm_runtime,
        timeout=gemini_call_timeout_seconds,
        llm_model=llm_runtime.GEMINI_MODEL_DEEP,
        compile_fields=compile_field_understanding,
    )
