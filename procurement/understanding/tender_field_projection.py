"""Explicit compatibility facade for tender field projections."""

from __future__ import annotations

import logging
import re
from typing import Any

# Keep the historical dependency names available for direct imports and
# patch targets. Implementations live in focused sibling modules.
from procurement.understanding.requirement_evidence import (
    _COMPOSITE_REQUIREMENT_FIELDS,
    _atomic_value_has_source_table_ambiguity,
    _context_for_atomic_value,
    _criterion_suffix,
    _default_evidence_for_atomic_value,
    _evidence_for_atomic_value,
    _evidence_groups,
    _requirement_semantics,
    _requirement_semantic_key,
    _requirement_values,
    _source_references_for_atomic_value,
    evidence_requirements_for_field,
)
from .tender_field_projection_controls import (
    _EXPLICIT_CONTROL_PATTERNS,
    _GEM_BID_RE,
    _GENERIC_IDENTITY_VALUES,
    _NO_EXEMPTION_PATTERNS,
    _explicit_control_matches,
    _generic_identity_value,
    _identity_candidate,
    _issuer_from_source_units,
    _project_canonical_gem_identity,
    _project_deterministic_tender_controls,
    _project_source_table_ambiguities,
)
from .tender_field_projection_fields import (
    _canonicalize_field_evidence,
    _project_resolved_tender_summary,
    _refresh_understanding_status,
    _restore_serialized_collection_values,
    field_entries,
    profile_from_fields,
    summary_from_resolved_fields,
)
from .tender_field_projection_provenance import (
    _SOURCE_REFERENCE_METADATA,
    _attach_matrix_provenance,
    _canonical_source_reference,
    _canonical_source_references,
    _canonical_source_units,
    _provenance_value,
    _source_unit_for_reference,
)
from .tender_field_projection_requirements import (
    _REQUIREMENT_FIELDS,
    requirement_matrix_from_fields,
)

_SHARED_CALL_BUDGET_NOTE = "Focused-understanding call budget was exhausted."
logger = logging.getLogger("procurement.understanding")

__all__ = [
    "logging",
    "re",
    "Any",
    "logger",
    "evidence_requirements_for_field",
    "field_entries",
    "summary_from_resolved_fields",
    "profile_from_fields",
    "requirement_matrix_from_fields",
]
