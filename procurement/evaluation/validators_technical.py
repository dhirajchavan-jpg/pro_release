"""Technical capability and product-identity validators."""

from __future__ import annotations

from typing import Any

from procurement.domain.schemas import CriterionValidatorResult, RequirementMatrixItem
from procurement.evaluation.criteria import criterion_atomic_requirement_text
from procurement.evaluation.facts import exact_product_identity_anchors
from .validators_shared import financial_amount_values, threshold_from_requirement, validator_result


def validate_similar_work_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    threshold = threshold_from_requirement(item)
    amounts = financial_amount_values(text)
    has_order = any(
        term in text
        for term in ["purchase order", "work order", "letter of intent", "loi", "award of work"]
    )
    has_execution = any(
        term in text
        for term in [
            "completion certificate",
            "work completion",
            "tax invoice",
            "final bill",
            "service entry",
            "executed value",
            "completed",
        ]
    )
    checks = {
        "order_evidence": "pass" if has_order else "missing",
        "execution_evidence": "pass" if has_execution else "missing",
    }
    if threshold and amounts:
        checks["value_threshold"] = "pass" if max(amounts) >= threshold else "fail"
    if checks.get("value_threshold") == "fail":
        return validator_result(
            item,
            "fail",
            "Similar-work evidence value is below the required threshold.",
            checks,
            0.88,
            True,
            candidate,
        )
    if has_order and has_execution and checks.get("value_threshold", "pass") == "pass":
        return validator_result(
            item,
            "pass",
            "Similar-work order, execution proof, and value checks are supported.",
            checks,
            0.86,
            False,
            candidate,
        )
    if has_order or has_execution:
        return validator_result(
            item,
            "unclear",
            "Similar-work evidence is partial; order/execution linkage is incomplete.",
            checks,
            0.65,
            False,
            candidate,
        )
    return validator_result(
        item,
        "missing",
        "Required similar-work order/execution evidence was not found.",
        checks,
        0.75,
        False,
        candidate,
        missing_documents=["Similar-work order/execution evidence"],
    )

def validate_product_identity_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    anchors = exact_product_identity_anchors(criterion_atomic_requirement_text(item))
    matched = [anchor for anchor in anchors if anchor in text]
    checks = {
        "product_identity": (
            "pass" if len(anchors) >= 2 and len(matched) == len(anchors) else "unclear"
        )
    }
    if checks["product_identity"] == "pass":
        return validator_result(
            item,
            "pass",
            f"Offered product identity matches the tender item and specification ({', '.join(anchors)}).",
            checks,
            0.98,
            False,
            candidate,
        )
    return validator_result(
        item,
        "unchanged",
        "Exact tender item/specification identifiers were not all found in bidder evidence.",
        checks,
        0.0,
        False,
        candidate,
    )

__all__ = [
    "validate_product_identity_criterion",
    "validate_similar_work_criterion",
]
