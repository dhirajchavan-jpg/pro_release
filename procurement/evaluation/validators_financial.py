"""Financial and accounting evidence validators."""

from __future__ import annotations

from typing import Any

from procurement.domain.schemas import CriterionValidatorResult, RequirementMatrixItem
from .validators_shared import (
    explicitly_incomplete_financial_years,
    financial_amount_values,
    financial_year_signal_count,
    has_udin_signal,
    requires_financial_years,
    threshold_from_requirement,
    validator_result,
)


def validate_turnover_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    threshold = threshold_from_requirement(item)
    amounts = financial_amount_values(text)
    year_count = financial_year_signal_count(text)
    requires_three_years = requires_financial_years(item)
    checks = {
        "turnover_values": "pass" if amounts else "unclear",
        "financial_years": "pass" if not requires_three_years or year_count >= 3 else "fail",
    }
    if requires_three_years and explicitly_incomplete_financial_years(text):
        return validator_result(
            item,
            "fail",
            "Financial evidence explicitly covers fewer than the required financial years.",
            checks,
            0.95,
            True,
            candidate,
        )
    if requires_three_years and 0 < year_count < 3:
        return validator_result(
            item,
            "fail",
            "Financial evidence covers fewer than the required financial years.",
            checks,
            0.9,
            True,
            candidate,
        )
    if threshold and amounts:
        top_values = sorted(amounts, reverse=True)[:3]
        average = sum(top_values) / len(top_values)
        checks["value_threshold"] = (
            "pass" if max(amounts) >= threshold or average >= threshold else "fail"
        )
        if checks["value_threshold"] == "fail":
            return validator_result(
                item,
                "fail",
                "Turnover/financial values are below the required threshold.",
                checks,
                0.9,
                True,
                candidate,
            )
        return validator_result(
            item,
            "pass",
            "Turnover evidence meets the required value/year checks.",
            checks,
            0.88,
            False,
            candidate,
        )
    if amounts or "turnover" in text:
        return validator_result(
            item,
            "unclear",
            "Turnover evidence is present but value threshold could not be deterministically verified.",
            checks,
            0.65,
            False,
            candidate,
        )
    return validator_result(
        item,
        "unchanged",
        "No turnover evidence signal found.",
        {},
        0.0,
        False,
        candidate,
    )

def validate_udin_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    requires_years = requires_financial_years(item)
    year_count = financial_year_signal_count(text)
    checks = {
        "udin": "pass" if has_udin_signal(text) else "fail",
        "financial_years": "pass" if not requires_years or year_count >= 3 else "fail",
    }
    if explicitly_incomplete_financial_years(text) or (
        requires_years and 0 < year_count < 3
    ):
        return validator_result(
            item,
            "fail",
            "UDIN/financial evidence does not cover the required financial years.",
            checks,
            0.95,
            True,
            candidate,
        )
    if has_udin_signal(text) and checks["financial_years"] == "pass":
        return validator_result(
            item,
            "pass",
            "UDIN-backed financial evidence is present.",
            checks,
            0.9,
            False,
            candidate,
        )
    return validator_result(
        item,
        "missing",
        "Required UDIN-backed financial evidence was not found.",
        checks,
        0.82,
        False,
        candidate,
        missing_documents=["UDIN-backed financial evidence"],
    )

def validate_net_worth_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    if "negative net worth" in text or ("net worth" in text and "negative" in text):
        return validator_result(
            item,
            "fail",
            "Net worth evidence indicates negative net worth.",
            {"net_worth": "fail"},
            0.9,
            True,
            candidate,
        )
    if "net worth" in text and any(
        term in text for term in ["positive", "profit", "not eroded", "greater than zero"]
    ):
        return validator_result(
            item,
            "pass",
            "Positive net worth evidence is present.",
            {"net_worth": "pass"},
            0.82,
            False,
            candidate,
        )
    if "net worth" in text or "networth" in text:
        return validator_result(
            item,
            "unclear",
            "Net worth evidence is present but positivity could not be verified.",
            {"net_worth": "unclear"},
            0.65,
            False,
            candidate,
        )
    return validator_result(
        item,
        "unchanged",
        "No net worth evidence signal found.",
        {},
        0.0,
        False,
        candidate,
    )

__all__ = [
    "validate_net_worth_criterion",
    "validate_turnover_criterion",
    "validate_udin_criterion",
]
