"""Compatibility facade for the configuration-driven BHEL evaluation extension.

The implementation is split into profile, source, matrix, and result modules.
These wrappers keep the historical public signatures and module-level seams
used by repository callers and integrations.
"""

from __future__ import annotations

from typing import Any, Iterable

from .bhel_evaluation_common import (
    _BASELINE_SEMANTIC_KEYS,
    _BLOCKING,
    _DISABLED,
    _EVALUATION_SCOPES,
    _FINDING_LIST_KEYS,
    _ID_FIELDS,
    _PROFILE_FILE,
    _PROFILE_NAMES,
    _SOURCE_FIELDS,
    _SOURCE_REFERENCE_KEYS,
    _SOURCE_WRAPPER_KEYS,
    _UNDERSTANDING_CONTAINERS,
    _mapping,
    _mapping_list,
    _model_data,
    _text,
    RESULT_STATUSES,
)
from .bhel_evaluation_matrix import (
    _append_bhel_items,
    _bhel_extension,
    _candidate_items,
    _criterion_id,
    _dynamic_items,
    _id_aliases,
    _requirement_item,
    _required_evidence,
    _source_quote,
    merge_bhel_profile as _merge_bhel_profile_impl,
)
from .bhel_evaluation_profile import (
    _amount_in_inr,
    _applicability_rule_allows,
    _evaluation_scope,
    _profile_path,
    _resolve_profile_file,
    _reviewed_field_value,
    _valid_profile,
    load_bhel_profile as _load_bhel_profile_impl,
)
from .bhel_evaluation_results import (
    _candidate_finding,
    _check_record,
    _finding_evidence,
    _finding_id,
    _finding_index,
    _finding_reason,
    _finding_records,
    _finding_value,
    _has_evidence,
    _normalized_result,
    _report_extension,
    _summary,
    apply_bhel_human_reviews as _apply_bhel_human_reviews_impl,
    bhel_comparative_summary as _bhel_comparative_summary_impl,
    build_bhel_extension as _build_bhel_extension_impl,
)
from .bhel_evaluation_sources import (
    _dynamic_source_refs,
    _explicit_sources,
    _iter_strings,
    _matching_references,
    _normalize_source_references,
    _source_grounded,
    _source_index,
    _source_records,
    _source_reference,
    _term_present,
)


def load_bhel_profile() -> dict | None:
    """Load the profile only when explicitly enabled through environment."""
    return _load_bhel_profile_impl()


def merge_bhel_profile(matrix: dict, understanding: dict, units: dict) -> dict:
    """Add a BHEL extension without changing the caller's matrix in place."""
    return _merge_bhel_profile_impl(
        matrix,
        understanding,
        units,
        load_profile=load_bhel_profile,
    )


def apply_bhel_human_reviews(extension: dict[str, Any], reviews: Iterable[Any]) -> dict[str, Any]:
    """Overlay latest immutable human decisions on a copied BHEL result."""
    return _apply_bhel_human_reviews_impl(extension, reviews, summary_fn=_summary)


def build_bhel_extension(matrix: dict, findings: Any, report: dict) -> dict:
    """Normalize one bidder report into a BHEL-specific, auditable extension."""
    return _build_bhel_extension_impl(
        matrix,
        findings,
        report,
        load_profile=load_bhel_profile,
        merge_profile=merge_bhel_profile,
        summary_fn=_summary,
    )


def bhel_comparative_summary(reports: dict[str, dict]) -> dict:
    """Summarize independent bidder extensions without mixing internal controls."""
    return _bhel_comparative_summary_impl(
        reports,
        load_profile=load_bhel_profile,
        summary_fn=_summary,
    )
