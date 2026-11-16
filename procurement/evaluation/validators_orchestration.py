"""Deterministic validator dispatch and finding application."""

from __future__ import annotations

from typing import Any

from procurement.domain.schemas import (
    CriteriaFinding,
    CriterionValidatorResult,
    CriterionValidatorSet,
    RequirementMatrixItem,
)
from procurement.domain.text import normalize_for_match, unique_items
from procurement.evaluation.criteria import criterion_allows_alternative_route
from .validators_shared import append_validator_evidence, merge_validator_checks, validator_result
from .validators_compliance import (
    validate_declaration_criterion,
    validate_emd_criterion,
    validate_local_content_criterion,
    validate_mse_criterion,
    validate_third_party_assurance_criterion,
)
from .validators_financial import (
    validate_net_worth_criterion,
    validate_turnover_criterion,
    validate_udin_criterion,
)
from .validators_statutory import validate_statutory_identifier_criterion
from .validators_technical import validate_product_identity_criterion, validate_similar_work_criterion


def validate_criterion_deterministically(
    item: RequirementMatrixItem,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    kinds = candidate.get("kinds", [])
    if not kinds:
        return validator_result(
            item,
            "unchanged",
            "No deterministic validator applies to this criterion.",
            {},
            0.0,
            False,
            candidate,
        )

    text = normalize_for_match(str(candidate.get("text") or ""))
    if not text:
        return validator_result(
            item,
            "unchanged",
            "No readable candidate evidence for deterministic validation.",
            {},
            0.0,
            False,
            candidate,
        )

    rule_results: list[CriterionValidatorResult] = []
    for kind in kinds:
        result = {
            "turnover": validate_turnover_criterion,
            "udin": validate_udin_criterion,
            "net_worth": validate_net_worth_criterion,
            "statutory_identifier": validate_statutory_identifier_criterion,
            "emd": validate_emd_criterion,
            "mse": validate_mse_criterion,
            "local_content": validate_local_content_criterion,
            "similar_work": validate_similar_work_criterion,
            "third_party_assurance": validate_third_party_assurance_criterion,
            "declaration": validate_declaration_criterion,
            "product_identity": validate_product_identity_criterion,
        }[kind](item, text, candidate)
        if result.status != "unchanged":
            rule_results.append(result)

    if not rule_results:
        return validator_result(
            item,
            "unchanged",
            "No deterministic signal was strong enough to change the result.",
            {},
            0.0,
            False,
            candidate,
        )

    failures = [result for result in rule_results if result.status in {"fail", "missing"}]
    passes = [result for result in rule_results if result.status == "pass"]
    if (
        passes
        and criterion_allows_alternative_route(item)
        and not any(result.hard_failure for result in failures)
    ):
        return validator_result(
            item,
            "pass",
            "; ".join(result.reason for result in passes),
            merge_validator_checks(passes),
            max(result.confidence for result in passes),
            False,
            candidate,
        )
    if failures:
        status = "fail" if any(result.status == "fail" for result in failures) else "missing"
        return validator_result(
            item,
            status,
            "; ".join(result.reason for result in failures),
            merge_validator_checks(failures),
            max(result.confidence for result in failures),
            any(result.hard_failure for result in failures),
            candidate,
            missing_documents=unique_items(
                [doc for result in failures for doc in result.missing_documents]
            ),
        )
    unclear = [result for result in rule_results if result.status == "unclear"]
    if unclear:
        return validator_result(
            item,
            "unclear",
            "; ".join(result.reason for result in unclear),
            merge_validator_checks(unclear),
            max(result.confidence for result in unclear),
            False,
            candidate,
        )
    return validator_result(
        item,
        "pass",
        "; ".join(result.reason for result in passes),
        merge_validator_checks(passes),
        min(result.confidence for result in passes) if passes else 0.0,
        False,
        candidate,
    )

def apply_criterion_validator_results(
    findings: list[CriteriaFinding],
    validator_set: CriterionValidatorSet,
    items: list[RequirementMatrixItem],
) -> None:
    findings_by_id = {finding.criterion_id: finding for finding in findings}
    item_by_id = {item.criterion_id: item for item in items}
    for result in validator_set.results:
        finding = findings_by_id.get(result.criterion_id)
        if not finding or result.status == "unchanged":
            continue
        if result.status == "pass":
            safe_objective_pass = (
                result.checks.get("entity_scoped_identifier_proof") == "pass"
                or result.checks.get("local_content") == "pass"
                or result.checks.get("product_identity") == "pass"
            ) and bool(result.page_ids)
            if safe_objective_pass and finding.status in {
                "missing",
                "unclear",
                "not_evaluated",
            }:
                finding.status = "pass"
                finding.reason = result.reason
                finding.missing_documents = []
                append_validator_evidence(finding, result)
            continue
        if result.status == "not_evaluated":
            finding.status = "not_evaluated"
            finding.reason = result.reason
            finding.missing_documents = []
            append_validator_evidence(finding, result)
            continue
        if result.status in {"fail", "missing"}:
            if (
                finding.status in {"fail", "missing", "not_evaluated"}
                or result.hard_failure
                or "local_content" in result.checks
            ):
                finding.status = result.status  # type: ignore[assignment]
                finding.reason = result.reason
                if result.missing_documents:
                    finding.missing_documents = result.missing_documents
                elif result.status == "missing":
                    item = item_by_id.get(result.criterion_id)
                    finding.missing_documents = (
                        [
                            evidence.document_name
                            for evidence in item.required_evidence
                            if evidence.required
                        ]
                        if item
                        else finding.required_documents
                    )
                append_validator_evidence(finding, result)
            continue
        if result.status == "unclear" and (
            finding.status in {"missing", "not_evaluated"}
            or (finding.status == "pass" and "local_content" in result.checks)
        ):
            finding.status = "unclear"
            finding.reason = result.reason
            finding.missing_documents = []
            append_validator_evidence(finding, result)

__all__ = [
    "apply_criterion_validator_results",
    "validate_criterion_deterministically",
]
