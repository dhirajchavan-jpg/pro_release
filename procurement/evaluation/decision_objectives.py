"""Objective-search normalization for exact, evidence-backed criteria."""

from __future__ import annotations

from dataclasses import replace
import re
from collections.abc import Mapping
from typing import Any

from .decision_records import CriterionDefinition, CriterionResult


_EXACT_OBJECTIVE_MARKERS = (
    " pan ",
    " gstin ",
    " permanent account number",
    " certificate number",
    " registration number",
    " license number",
    " licence number",
    " udyam ",
    " cin ",
    " identifier",
)
_BROAD_OBJECTIVE_MARKERS = (
    " experience",
    " similar work",
    " past project",
    " implementation",
    " maintenance",
    " capacity",
    " turnover",
    " net worth",
    " multiple",
    " combined",
    " across documents",
    " all documents",
)
_OBJECTIVE_IDENTIFIER_PATTERNS = (
    re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE),
    re.compile(r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:certificate|registration|licen[cs]e|reference|serial)\s*"
        r"(?:no\.?|number|id|#)\s*[:\-]?\s*"
        r"(?=[A-Z0-9./_-]*\d)[A-Z0-9][A-Z0-9./_-]{2,}\b",
        re.IGNORECASE,
    ),
)
_OBJECTIVE_POSITIVE_MARKERS = (
    "passed",
    "verified",
    "valid",
    "confirmed",
    "matches",
    "matched",
    "satisfied",
    "present",
)
_OBJECTIVE_NEGATIVE_MARKERS = (
    "not found",
    "missing",
    "invalid",
    "mismatch",
    "not present",
    "not valid",
    "not verified",
    "unverified",
    "could not",
    "unable",
    "unclear",
    "needs review",
    "review required",
    "not met",
    "failed",
)
_SEARCH_COVERAGE_MARKERS = (
    "search incomplete",
    "remain uninspected",
    "remains uninspected",
    "uninspected pages",
    "unseen pages",
    "bounded recovery",
)


def _normalized_text(value: Any) -> str:
    return " " + " ".join(str(value or "").casefold().split()) + " "


def _mapping_has_verified_flag(value: Mapping[str, Any]) -> bool:
    for key in ("verified", "exact_match", "identifier_verified", "grounded"):
        raw = value.get(key)
        if raw is True or str(raw or "").strip().casefold() in {"true", "yes", "pass", "passed", "verified"}:
            return True
    return False


def _contains_objective_identifier(value: Any) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in _OBJECTIVE_IDENTIFIER_PATTERNS)


def _is_exact_objective_criterion(criterion: CriterionDefinition) -> bool:
    metadata = dict(criterion.metadata or {})
    objective = str(
        metadata.get("objective")
        or metadata.get("search_objective")
        or metadata.get("profile")
        or ""
    ).strip().casefold()
    if objective in {"broad", "multi_document", "experience", "capability"}:
        return False
    if bool(metadata.get("multi_document")) or len(criterion.evidence_requirements) > 1:
        return False
    text = _normalized_text(
        " ".join(
            (
                criterion.requirement,
                criterion.pass_rule,
                criterion.category,
                *criterion.evidence_requirements,
            )
        )
    )
    if any(marker in text for marker in _BROAD_OBJECTIVE_MARKERS):
        return False
    return objective in {"exact", "identifier", "statutory", "objective"} or any(
        marker in text for marker in _EXACT_OBJECTIVE_MARKERS
    )


def _objective_proof_is_verified(
    criterion: CriterionDefinition,
    result: CriterionResult,
) -> bool:
    """Recognize a grounded exact proof without weakening broad searches."""

    if result.state != "search_incomplete" or result.human_review is not None:
        return False
    if not _is_exact_objective_criterion(criterion):
        return False

    assessment = result.effective_assessment
    quotes = " ".join(ref.quote or "" for ref in assessment.evidence_refs)
    if not _contains_objective_identifier(quotes):
        return False

    facts = dict(assessment.facts or {})
    metadata = dict(assessment.metadata or {})
    proof_text = _normalized_text(
        " ".join(
            (
                assessment.objective_answer,
                assessment.reason,
                quotes,
                " ".join(str(key) + " " + str(value) for key, value in facts.items()),
                " ".join(str(key) + " " + str(value) for key, value in metadata.items()),
            )
        )
    )
    for marker in _SEARCH_COVERAGE_MARKERS:
        proof_text = proof_text.replace(_normalized_text(marker), " ")
    if any(marker in proof_text for marker in _OBJECTIVE_NEGATIVE_MARKERS):
        return False
    return _mapping_has_verified_flag(facts) or _mapping_has_verified_flag(metadata) or any(
        marker in proof_text for marker in _OBJECTIVE_POSITIVE_MARKERS
    )


def _normalize_objective_search_result(
    criterion: CriterionDefinition,
    result: CriterionResult,
) -> CriterionResult:
    if not _objective_proof_is_verified(criterion, result):
        return result
    assessment = result.effective_assessment
    reason = assessment.reason.rstrip()
    suffix = "Exact objective proof is sufficient; unrelated uninspected pages do not change this criterion."
    if suffix.casefold() not in reason.casefold():
        reason = f"{reason.rstrip('.')} {suffix}".strip()
    promoted = assessment.with_state("passed", reason=reason)
    return replace(result, ai_assessment=promoted, effective_assessment=promoted)


__all__ = [
    "_EXACT_OBJECTIVE_MARKERS",
    "_BROAD_OBJECTIVE_MARKERS",
    "_OBJECTIVE_IDENTIFIER_PATTERNS",
    "_OBJECTIVE_POSITIVE_MARKERS",
    "_OBJECTIVE_NEGATIVE_MARKERS",
    "_SEARCH_COVERAGE_MARKERS",
    "_normalized_text",
    "_mapping_has_verified_flag",
    "_contains_objective_identifier",
    "_is_exact_objective_criterion",
    "_objective_proof_is_verified",
    "_normalize_objective_search_result",
]
