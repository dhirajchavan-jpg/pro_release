"""Shared deterministic validator helpers and financial parsing."""

from __future__ import annotations

import re
from typing import Any, Literal

from procurement.domain.schemas import (
    CriteriaFinding,
    CriterionValidatorResult,
    Evidence,
    EvidenceRequirement,
    PageUnit,
    RequirementMatrixItem,
)
from procurement.domain.text import normalize_for_match, unique_items


def has_udin_signal(text: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    return bool(re.search(r"\budin\b", text.lower())) or bool(
        re.search(r"\d{8}[a-z0-9]{10}", compact)
    )

def financial_year_signal_count(text: str) -> int:
    return len(financial_year_keys(text))

def financial_year_keys(text: str) -> set[int]:
    """Normalize fiscal-year spellings to their ending calendar year."""
    normalized = normalize_for_match(text)
    years: set[int] = set()
    for match in re.finditer(r"\b(20\d{2})\s*[-/]\s*(\d{2,4})\b", normalized):
        first = int(match.group(1))
        raw_second = match.group(2)
        second = (
            int(raw_second)
            if len(raw_second) == 4
            else (first // 100) * 100 + int(raw_second)
        )
        if second == first + 1:
            years.add(second)
    years.update(
        int(value)
        for value in re.findall(r"\b31(?:st)?\s+march[,.]?\s+(20\d{2})\b", normalized)
    )
    return years

def normalize_check_key(check: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", check.lower()).strip("_")

def financial_check_coverage_pages(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    pages: list[PageUnit],
    *,
    max_pages: int = 4,
) -> list[PageUnit]:
    """Select the smallest set of statement pages covering the required years."""
    check_names = {normalize_check_key(check) for check in evidence.checks}
    if "financial_years" not in check_names and not requires_financial_years(item):
        return []
    requirement = normalize_for_match(
        f"{item.requirement} {item.pass_rule} {evidence.document_name} {evidence.acceptance_rule}"
    )
    requires_turnover = any(marker in requirement for marker in ("turnover", "annual revenue"))
    candidates: list[tuple[set[int], int, PageUnit]] = []
    for page in pages:
        text = normalize_for_match(page.text)
        years = financial_year_keys(text)
        if not years:
            continue
        has_turnover_values = any(
            marker in text
            for marker in ("revenue from operations", "turnover", " sales ", "statement of profit")
        )
        has_financial_statement = has_turnover_values or any(
            marker in text
            for marker in ("balance sheet", "profit and loss", "schedules forming part of the accounts")
        )
        if not has_financial_statement or (requires_turnover and not has_turnover_values):
            continue
        score = (
            8 * int("revenue from operations" in text)
            + 7 * int("statement of profit" in text)
            + 6 * int("schedules forming part of the accounts" in text)
            + 3 * int("turnover" in text)
            + 2 * int("audited" in text)
            + len(years)
        )
        candidates.append((years, score, page))
    if not candidates:
        return []
    target_years = set(sorted({year for years, _score, _page in candidates for year in years}, reverse=True)[:3])
    remaining = set(target_years)
    selected: list[PageUnit] = []
    unused = list(candidates)
    while remaining and unused and len(selected) < max_pages:
        unused.sort(
            key=lambda value: (
                len(value[0] & remaining),
                value[1],
                value[2].text_chars,
                value[2].page_id,
            ),
            reverse=True,
        )
        years, _score, page = unused.pop(0)
        covered = years & remaining
        if not covered:
            break
        selected.append(page)
        remaining -= covered
    return selected

def append_validator_evidence(
    finding: CriteriaFinding,
    result: CriterionValidatorResult,
) -> None:
    existing_pages = {
        (evidence.document_id, evidence.page)
        for evidence in finding.evidence
        if evidence.document_id and evidence.page
    }
    covered_documents = {
        evidence.document_id for evidence in finding.evidence if evidence.document_id
    }
    for page_id in result.page_ids:
        document_id, separator, page_text = page_id.rpartition("-p")
        if not separator or not document_id or not page_text.isdigit():
            continue
        page = int(page_text)
        if page < 1 or (document_id, page) in existing_pages:
            continue
        finding.evidence.append(
            Evidence(document_id=document_id, page=page, confidence=result.confidence)
        )
        existing_pages.add((document_id, page))
        covered_documents.add(document_id)
    for document_id in result.evidence_document_ids:
        if document_id in covered_documents:
            continue
        finding.evidence.append(
            Evidence(document_id=document_id, confidence=result.confidence)
        )
        covered_documents.add(document_id)

def validator_result(
    item: RequirementMatrixItem,
    status: Literal["pass", "fail", "missing", "unclear", "not_evaluated", "unchanged"],
    reason: str,
    checks: dict[str, str],
    confidence: float,
    hard_failure: bool,
    candidate: dict[str, Any],
    missing_documents: list[str] | None = None,
) -> CriterionValidatorResult:
    return CriterionValidatorResult(
        criterion_id=item.criterion_id,
        status=status,
        reason=reason,
        checks=checks,
        confidence=confidence,
        hard_failure=hard_failure,
        evidence_documents=list(candidate.get("documents") or []),
        evidence_document_ids=list(candidate.get("document_ids") or []),
        page_ids=list(candidate.get("page_ids") or []),
        missing_documents=missing_documents or [],
    )

def merge_validator_checks(results: list[CriterionValidatorResult]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for result in results:
        merged.update(result.checks)
    return merged

def threshold_from_requirement(item: RequirementMatrixItem) -> float | None:
    values = parse_money_amounts(
        normalize_for_match(f"{item.requirement} {item.source_quote or ''}")
    )
    return max(values) if values else None

def parse_money_amounts(text: str) -> list[float]:
    values: list[float] = []
    for number, unit in re.findall(
        r"(?:inr|rs\.?)\s*([0-9][0-9,]*(?:\.\d+)?)\s*(lakh|lakhs|crore|crores|cr)?",
        text,
    ):
        values.append(scale_money_number(number, unit))
    for number, unit in re.findall(
        r"\b([0-9][0-9,]*(?:\.\d+)?)\s*(lakh|lakhs|crore|crores|cr)\b",
        text,
    ):
        values.append(scale_money_number(number, unit))
    return [value for value in values if value > 0]

def financial_amount_values(text: str) -> list[float]:
    values = parse_money_amounts(text)
    if not any(
        term in text
        for term in [
            "turnover",
            "value",
            "amount",
            "net worth",
            "purchase order",
            "work order",
            "invoice",
            "audited",
        ]
    ):
        return unique_float_values(values)
    scale = 1.0
    if any(
        term in text
        for term in ["in hundred", "in hundreds", "figures in hundred", "amount in hundred"]
    ):
        scale = 100.0
    elif any(
        term in text for term in ["in lakh", "in lakhs", "rs in lakh", "rs. in lakh"]
    ):
        scale = 100000.0
    elif any(
        term in text for term in ["in crore", "in crores", "rs in crore", "rs. in crore"]
    ):
        scale = 10000000.0
    for raw in re.findall(
        r"\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b|\b\d{5,}(?:\.\d+)?\b",
        text,
    ):
        value = float(raw.replace(",", ""))
        if 1900 <= value <= 2099:
            continue
        if scale == 1.0 and value < 10000:
            continue
        values.append(value * scale)
    return unique_float_values(values)

def scale_money_number(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    unit = (unit or "").lower()
    if unit in {"lakh", "lakhs"}:
        return value * 100000
    if unit in {"crore", "crores", "cr"}:
        return value * 10000000
    return value

def unique_float_values(values: list[float]) -> list[float]:
    seen: set[int] = set()
    output: list[float] = []
    for value in values:
        rounded = int(round(value))
        if rounded in seen:
            continue
        seen.add(rounded)
        output.append(value)
    return output

def requires_financial_years(item: RequirementMatrixItem) -> bool:
    text = normalize_for_match(
        f"{item.requirement} {item.pass_rule} {item.source_quote or ''} "
        f"{' '.join(evidence.document_name for evidence in item.required_evidence)} "
        f"{' '.join(' '.join(evidence.checks) for evidence in item.required_evidence)}"
    )
    return any(
        term in text
        for term in [
            "three financial years",
            "3 financial years",
            "last three years",
            "last 3 years",
            "financial_years",
        ]
    )

def explicitly_incomplete_financial_years(text: str) -> bool:
    return any(
        term in text
        for term in [
            "less than 3 years",
            "less than three years",
            "not having audited annual reports for 3 years",
            "not having audited annual reports for three years",
            "only one financial year",
            "only two financial years",
        ]
    )

__all__ = [
    "append_validator_evidence",
    "explicitly_incomplete_financial_years",
    "financial_amount_values",
    "financial_check_coverage_pages",
    "financial_year_keys",
    "financial_year_signal_count",
    "has_udin_signal",
    "merge_validator_checks",
    "normalize_check_key",
    "parse_money_amounts",
    "requires_financial_years",
    "scale_money_number",
    "threshold_from_requirement",
    "unique_float_values",
    "validator_result",
]
