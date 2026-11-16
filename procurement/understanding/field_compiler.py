"""Stable import facade for the modular tender-field compiler.

The public import path remains unchanged while implementation responsibilities
live in small, auditable modules.
"""

from .field_amendments import compile_package_amendments
from .field_retrieval import LocalFieldRetriever
from .field_results import (
    _explicit_not_applicable_value,
    _normalize_result,
    _project_controlling_total_marks,
    _project_explicit_formulas,
)
from .field_schema import (
    _prefer_more_complete_found,
    _project_grounded_identifier,
    _project_unique_labeled_value,
)
from .field_understanding import compile_field_understanding
from .schema import (
    RetrievalGuidance,
    RfpFieldSpec,
    RfpSectionSpec,
    RfpUnderstandingSchema,
)


__all__ = [
    "LocalFieldRetriever",
    "RetrievalGuidance",
    "RfpFieldSpec",
    "RfpSectionSpec",
    "RfpUnderstandingSchema",
    "_explicit_not_applicable_value",
    "_normalize_result",
    "_prefer_more_complete_found",
    "_project_controlling_total_marks",
    "_project_explicit_formulas",
    "_project_grounded_identifier",
    "_project_unique_labeled_value",
    "compile_field_understanding",
    "compile_package_amendments",
]
