"""Explicit compatibility facade for modular field-result helpers."""

from __future__ import annotations

import json
import re
from typing import Any

# Keep the historical dependency names available on this module for direct
# imports and patch targets. Implementations live in the field_results_* modules.
from .field_core import (
    _CONDITION_CUES,
    _DOCUMENT_CUES,
    _EXCEPTION_CUES,
    _NUMBER_WORDS,
    _PROOF_QUERY,
    _UPDATE_QUERY,
    _binding_text_match,
    _clean_list,
    _compact_text,
    _configured_anchor_match,
    _contains_phrase,
    _direct_field_match,
    _explicit_boolean_polarity,
    _formal_revision_cells,
    _has_ocr_near_token,
    _is_placeholder_value,
    _lexical_text,
    _needs_proof_lane,
    _numeric_token_compatible_for_field,
    _phrase_text,
    _requires_exhaustive_coverage,
    _signal_phrase_covered,
    _table_cells,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_text,
    _value_matches,
)
from .field_retrieval import LocalFieldRetriever
from .schema import RfpFieldSpec
from .field_results_deterministic import (
    _deterministic_boolean_result,
    _deterministic_scalar_result,
)
from .field_results_evidence import (
    _date_value_grounded,
    _date_values,
    _explicit_not_applicable_value,
    _has_explicit_labeled_value,
    _hydrate_evidence,
    _money_value_grounded,
    _money_values,
    _request_details,
)
from .field_results_normalization import (
    _drop_serialized_collection_artifacts,
    _normalize_result,
    _promote_explicit_exceptions,
    _serialized_collection_artifact,
)
from .field_results_projection import (
    _explicit_total_mark_values,
    _project_controlling_scalar,
    _project_controlling_total_marks,
    _project_currency_markers,
    _project_deleted_members,
    _project_explicit_formulas,
    _project_prompt_exclusions,
    _project_template_conflict,
)
from .field_results_validation import (
    _BLOCKING_GROUNDING_PREFIXES,
    _blocking_grounding_issues,
    _compact_field_evidence_item,
    _completeness_issues,
    _completeness_query,
    _relevant_evidence_text,
)


__all__ = [
    "_hydrate_evidence",
    "_money_values",
    "_money_value_grounded",
    "_date_values",
    "_date_value_grounded",
    "_explicit_not_applicable_value",
    "_normalize_result",
    "_has_explicit_labeled_value",
    "_request_details",
    "_project_controlling_scalar",
    "_project_deleted_members",
    "_project_template_conflict",
    "_project_explicit_formulas",
    "_project_prompt_exclusions",
    "_project_currency_markers",
    "_serialized_collection_artifact",
    "_drop_serialized_collection_artifacts",
    "_explicit_total_mark_values",
    "_project_controlling_total_marks",
    "_promote_explicit_exceptions",
    "_deterministic_boolean_result",
    "_deterministic_scalar_result",
    "_relevant_evidence_text",
    "_completeness_issues",
    "_BLOCKING_GROUNDING_PREFIXES",
    "_blocking_grounding_issues",
    "_completeness_query",
    "_compact_field_evidence_item",
]
