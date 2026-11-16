"""Compatibility facade for deterministic bidder criterion validators.

The implementation is split by responsibility, while this module keeps the
historical import surface used by the application and external callers.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from procurement.domain.schemas import (
    CriteriaFinding,
    CriterionValidatorResult,
    CriterionValidatorSet,
    Evidence,
    EvidenceRequirement,
    PageUnit,
    RequirementMatrixItem,
)
from procurement.domain.text import (
    humanize_check_name,
    keyword_tokens,
    normalize_for_match,
    unique_items,
)
from procurement.evaluation.criteria import (
    criterion_allows_alternative_route,
    criterion_atomic_requirement_text,
)
from procurement.evaluation.evidence import (
    EntityRole as EvidenceEntityRole,
    EvidenceLedger as EntityEvidenceLedger,
    EvidenceObservation,
    IdentifierType,
    ProofContract as EntityProofContract,
    ProofStrength,
    ReconciliationStatus,
)
from procurement.evaluation.facts import (
    declared_local_content_percent,
    exact_product_identity_anchors,
    identifier_matches_by_role,
    unfilled_local_content_template,
)

from .validators_shared import (
    append_validator_evidence,
    explicitly_incomplete_financial_years,
    financial_amount_values,
    financial_check_coverage_pages,
    financial_year_keys,
    financial_year_signal_count,
    has_udin_signal,
    merge_validator_checks,
    normalize_check_key,
    parse_money_amounts,
    requires_financial_years,
    scale_money_number,
    threshold_from_requirement,
    unique_float_values,
    validator_result,
)
from .validators_financial import (
    validate_net_worth_criterion,
    validate_turnover_criterion,
    validate_udin_criterion,
)
from .validators_statutory import (
    identifier_artifact_required,
    required_identifier_documents,
    validate_statutory_identifier_criterion,
    validate_statutory_identifier_from_ledger,
)
from .validators_compliance import (
    udyam_enterprise_category,
    validate_declaration_criterion,
    validate_emd_criterion,
    validate_local_content_criterion,
    validate_mse_criterion,
    validate_third_party_assurance_criterion,
)
from .validators_technical import (
    validate_product_identity_criterion,
    validate_similar_work_criterion,
)
from .validators_orchestration import (
    apply_criterion_validator_results,
    validate_criterion_deterministically,
)

__all__ = [
    "append_validator_evidence",
    "apply_criterion_validator_results",
    "explicitly_incomplete_financial_years",
    "financial_amount_values",
    "financial_year_keys",
    "financial_year_signal_count",
    "has_udin_signal",
    "identifier_artifact_required",
    "merge_validator_checks",
    "parse_money_amounts",
    "required_identifier_documents",
    "requires_financial_years",
    "scale_money_number",
    "threshold_from_requirement",
    "udyam_enterprise_category",
    "unique_float_values",
    "validate_criterion_deterministically",
    "validate_declaration_criterion",
    "validate_emd_criterion",
    "validate_local_content_criterion",
    "validate_mse_criterion",
    "validate_net_worth_criterion",
    "validate_product_identity_criterion",
    "validate_similar_work_criterion",
    "validate_statutory_identifier_criterion",
    "validate_statutory_identifier_from_ledger",
    "validate_third_party_assurance_criterion",
    "validate_turnover_criterion",
    "validate_udin_criterion",
    "validator_result",
]
