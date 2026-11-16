"""Compatibility facade for local structural and lexical field retrieval."""

from __future__ import annotations

from collections import Counter
import math
import re
from typing import Any, Callable

from .field_core import (
    _DELETE_CUES,
    _EXPLICIT_UPDATE_CUES,
    _GATE_QUERY,
    _PROOF_QUERY,
    _UPDATE_QUERY,
    _amendment_source_binds_field,
    _compact_text,
    _configured_anchor_match,
    _contains_phrase,
    _direct_field_match,
    _direct_scalar_update_value,
    _field_presence_match,
    _field_subject_compatible,
    _field_target_match,
    _formal_revision_cells,
    _implicit_parallel_revision_cells,
    _is_explicit_full_replacement,
    _is_formal_revision_row,
    _is_replacement_scope,
    _issuer_response_text,
    _needs_document_start_lane,
    _needs_gate_lane,
    _needs_proof_lane,
    _paired_nested_revision_cells,
    _phrase_text,
    _pointer_only_amendment_response,
    _scope_contains,
    _section_scope_key,
    _table_cells,
    _table_header_index,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_reference_keys,
    _unit_text,
)
from .schema import RfpFieldSpec, RfpSectionSpec, RfpUnderstandingSchema

from .field_retrieval_index import FieldRetrievalIndex
from .field_retrieval_scoring import FieldRetrievalScoring
from .field_retrieval_state import FieldRetrievalState
from .field_retrieval_search import FieldRetrievalSearch


class LocalFieldRetriever(
    FieldRetrievalIndex,
    FieldRetrievalScoring,
    FieldRetrievalState,
    FieldRetrievalSearch,
):
    """BM25 retriever with structural expansion and optional semantic reranking."""


__all__ = ["LocalFieldRetriever"]
