"""Explicit compatibility facade for the canonical decision ledger.

The implementation is split by responsibility into semantics, records,
objective normalization, and ledger aggregation.  This module intentionally
keeps the historical import surface stable for existing pipeline consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from .decision_ledger import (
    CanonicalDecisionLedger,
    Criterion,
    CriterionCell,
    DecisionLedger,
    _ids,
    _result_groups,
    _status_summary,
    build_decision_ledger,
    canonical_hash,
    canonical_json,
    compatibility_projection,
)
from .decision_objectives import (
    _BROAD_OBJECTIVE_MARKERS,
    _EXACT_OBJECTIVE_MARKERS,
    _OBJECTIVE_IDENTIFIER_PATTERNS,
    _OBJECTIVE_NEGATIVE_MARKERS,
    _OBJECTIVE_POSITIVE_MARKERS,
    _SEARCH_COVERAGE_MARKERS,
    _contains_objective_identifier,
    _is_exact_objective_criterion,
    _mapping_has_verified_flag,
    _normalize_objective_search_result,
    _normalized_text,
    _objective_proof_is_verified,
)
from .decision_records import (
    Assessment,
    CriterionDefinition,
    CriterionResult,
    EvidenceRef,
    HumanReview,
)
from .decision_rules import criterion_blocks_financial_opening, criterion_blocks_qualification
from .decision_semantics import (
    APPLICABILITY_STATES,
    CRITERION_KINDS,
    FAILURE_STATES,
    INCOMPLETE_STATES,
    LIMITED_STATES,
    RESULT_STATES,
    SUCCESS_STATES,
    CriterionSetError,
    DecisionLedgerError,
    DuplicateCriterionError,
    EvidenceReferenceError,
    State,
    _NON_BLOCKING_EFFECTS,
    _NON_BLOCKING_MATERIALITIES,
    _as_mapping,
    _canonical_json,
    _first,
    _json_value,
    _semantic_key,
    _state,
    _text,
    _tuple_text,
    canonical_criterion_kind,
    criterion_has_nonblocking_semantics,
)


__all__ = [
    "APPLICABILITY_STATES",
    "canonical_criterion_kind",
    "CRITERION_KINDS",
    "RESULT_STATES",
    "Assessment",
    "CanonicalDecisionLedger",
    "Criterion",
    "CriterionCell",
    "CriterionDefinition",
    "CriterionResult",
    "CriterionSetError",
    "criterion_blocks_financial_opening",
    "criterion_blocks_qualification",
    "criterion_has_nonblocking_semantics",
    "DecisionLedger",
    "DecisionLedgerError",
    "DuplicateCriterionError",
    "EvidenceRef",
    "EvidenceReferenceError",
    "HumanReview",
    "build_decision_ledger",
    "canonical_hash",
    "canonical_json",
    "compatibility_projection",
]
