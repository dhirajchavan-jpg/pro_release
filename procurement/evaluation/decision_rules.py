"""Qualification and financial-opening rules for canonical criteria."""

from __future__ import annotations

from .decision_records import CriterionDefinition
from .decision_semantics import criterion_has_nonblocking_semantics


def criterion_blocks_qualification(criterion: CriterionDefinition) -> bool:
    """Return the only canonical predicate allowed to block qualification."""

    if not criterion.active or criterion.applicability == "not_applicable":
        return False
    if criterion_has_nonblocking_semantics(
        criterion.materiality,
        criterion.decision_effect,
    ):
        return False
    return (
        criterion.criterion_kind == "gate"
        and criterion.evaluation_scope
        not in {
            "post_award",
            "commercial_out_of_scope",
            "preference",
            "financial_opening",
            "internal_control",
        }
    )


def criterion_blocks_financial_opening(criterion: CriterionDefinition) -> bool:
    """Return whether a criterion can block the separate opening decision."""

    if not criterion.active or criterion.applicability == "not_applicable":
        return False
    if criterion_has_nonblocking_semantics(
        criterion.materiality,
        criterion.decision_effect,
    ):
        return False
    return (
        criterion.criterion_kind == "internal_gate"
        or criterion.evaluation_scope in {"financial_opening", "internal_control"}
        or criterion.decision_effect == "block_financial_opening"
    )


__all__ = [
    "criterion_blocks_qualification",
    "criterion_blocks_financial_opening",
]
