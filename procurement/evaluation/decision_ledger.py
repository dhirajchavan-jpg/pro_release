"""Canonical decision-ledger aggregation and compatibility projections."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .decision_objectives import _normalize_objective_search_result
from .decision_records import (
    Assessment,
    CriterionDefinition,
    CriterionResult,
    EvidenceRef,
    HumanReview,
)
from .decision_rules import criterion_blocks_financial_opening, criterion_blocks_qualification
from .decision_semantics import (
    FAILURE_STATES,
    INCOMPLETE_STATES,
    LIMITED_STATES,
    RESULT_STATES,
    SUCCESS_STATES,
    State,
    _canonical_json,
    _text,
    DecisionLedgerError,
    DuplicateCriterionError,
    EvidenceReferenceError,
    CriterionSetError,
)


def _ids(items: Sequence[Any]) -> list[str]:
    return [item.criterion_id for item in items]


def _result_groups(
    criteria: Sequence[CriterionDefinition],
    results: Mapping[str, CriterionResult],
    predicate: Any,
) -> tuple[list[CriterionDefinition], list[CriterionResult]]:
    selected = [criterion for criterion in criteria if criterion.active and predicate(criterion)]
    return selected, [results[criterion.criterion_id] for criterion in selected]


def _status_summary(
    criteria: Sequence[CriterionDefinition],
    results: Sequence[CriterionResult],
    *,
    state: str,
    blocking: Sequence[str] = (),
    review: Sequence[str] = (),
    reason: str = "",
) -> dict[str, Any]:
    return {
        "state": state,
        "criterion_ids": sorted(_ids(criteria)),
        "blocking_criterion_ids": sorted(blocking),
        "review_criterion_ids": sorted(review),
        "reason": reason,
        "evaluated": len(results),
    }


@dataclass(slots=True)
class DecisionLedger:
    """A complete criterion matrix and independently derived decision states."""

    criteria: tuple[CriterionDefinition, ...]
    results: tuple[CriterionResult, ...]
    tender_version_id: str = ""
    bid_id: str = ""
    schema_version: str = "procurement-decision-ledger.v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evaluation_completeness: dict[str, Any] = field(init=False)
    technical_qualification: dict[str, Any] = field(init=False)
    financial_opening: dict[str, Any] = field(init=False)
    score: dict[str, Any] = field(init=False)
    preference: dict[str, Any] = field(init=False)
    post_award: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        raw_criteria = tuple(CriterionDefinition.from_mapping(item) for item in self.criteria)
        raw_results = tuple(CriterionResult.from_mapping(item) for item in self.results)
        all_ids = [criterion.criterion_id for criterion in raw_criteria]
        duplicates = sorted({item for item in all_ids if all_ids.count(item) > 1})
        if duplicates:
            raise DuplicateCriterionError(f"duplicate frozen criterion_id(s): {duplicates}")
        active = tuple(sorted((item for item in raw_criteria if item.active), key=lambda x: x.criterion_id))
        result_ids = [result.criterion_id for result in raw_results]
        result_duplicates = sorted({item for item in result_ids if result_ids.count(item) > 1})
        if result_duplicates:
            raise DuplicateCriterionError(f"duplicate result criterion_id(s): {result_duplicates}")
        active_ids = {criterion.criterion_id for criterion in active}
        result_id_set = set(result_ids)
        unknown = sorted(result_id_set - active_ids)
        missing = sorted(active_ids - result_id_set)
        if unknown or missing:
            details = []
            if missing:
                details.append(f"missing active criterion_id(s): {missing}")
            if unknown:
                details.append(f"unexpected criterion_id(s): {unknown}")
            raise CriterionSetError("; ".join(details))
        by_id = {result.criterion_id: result for result in raw_results}
        ordered_results = tuple(
            _normalize_objective_search_result(criterion, by_id[criterion.criterion_id])
            for criterion in active
        )
        for criterion, result in zip(active, ordered_results):
            self._validate_result(criterion, result)
        self.criteria = active
        self.results = ordered_results
        self.tender_version_id = _text(self.tender_version_id)
        self.bid_id = _text(self.bid_id)
        self.recompute()

    @staticmethod
    def _validate_result(criterion: CriterionDefinition, result: CriterionResult) -> None:
        if result.state == "passed":
            if result.effective_assessment is not None and not result.effective_assessment.evidence_refs:
                raise EvidenceReferenceError(
                    f"{criterion.criterion_id} effective pass without exact evidence_refs"
                )
        if (
            result.human_review is None
            and result.ai_assessment.state == "passed"
            and not result.ai_assessment.evidence_refs
        ):
            raise EvidenceReferenceError(
                f"{criterion.criterion_id} ai pass without exact evidence_refs"
            )
        if criterion.criterion_kind == "scored" and result.score is not None and result.maximum_score is not None:
            if result.score < 0 or result.score > result.maximum_score:
                raise DecisionLedgerError(
                    f"{criterion.criterion_id} score must be between 0 and maximum_score"
                )

    @property
    def result_by_criterion_id(self) -> dict[str, CriterionResult]:
        return {result.criterion_id: result for result in self.results}

    def result_for(self, criterion_id: str) -> CriterionResult:
        try:
            return self.result_by_criterion_id[_text(criterion_id)]
        except KeyError as exc:
            raise KeyError(f"unknown criterion_id: {criterion_id!r}") from exc

    def recompute(self) -> DecisionLedger:
        result_map = self.result_by_criterion_id
        counts: dict[str, int] = {state: 0 for state in sorted(RESULT_STATES)}
        for result in self.results:
            counts[result.state] += 1
        incomplete_ids = sorted(
            result.criterion_id for result in self.results if result.state in INCOMPLETE_STATES
        )
        limited_ids = sorted(
            result.criterion_id for result in self.results if result.state in LIMITED_STATES
        )
        if incomplete_ids:
            completeness_state = "incomplete"
        elif limited_ids:
            completeness_state = "limited"
        else:
            completeness_state = "complete"
        self.evaluation_completeness = {
            "state": completeness_state,
            "active_criteria": len(self.criteria),
            "evaluated_criteria": len(self.criteria) - len(incomplete_ids),
            "counts": counts,
            "incomplete_criterion_ids": incomplete_ids,
            "limited_criterion_ids": limited_ids,
        }

        technical_criteria, technical_results = _result_groups(
            self.criteria,
            result_map,
            criterion_blocks_qualification,
        )
        self.technical_qualification = self._qualification_summary(
            technical_criteria, technical_results, label="technical qualification"
        )

        opening_criteria, opening_results = _result_groups(
            self.criteria,
            result_map,
            criterion_blocks_financial_opening,
        )
        technical_gate_ids = {criterion.criterion_id for criterion in technical_criteria}
        opening_ids = technical_gate_ids | {criterion.criterion_id for criterion in opening_criteria}
        opening_pairs = [
            (criterion, result_map[criterion.criterion_id])
            for criterion in self.criteria
            if criterion.criterion_id in opening_ids
        ]
        opening_failures = [
            criterion.criterion_id
            for criterion, result in opening_pairs
            if result.state in FAILURE_STATES
        ]
        opening_review = [
            criterion.criterion_id
            for criterion, result in opening_pairs
            if result.state not in SUCCESS_STATES | FAILURE_STATES
        ]
        if not opening_pairs:
            opening_state = "not_applicable"
            opening_reason = "No financial-opening or internal gate is configured."
        elif opening_failures:
            opening_state = "blocked"
            opening_reason = "One or more opening prerequisites failed or lacked required evidence."
        elif opening_review:
            opening_state = "pending"
            opening_reason = "Opening prerequisites require completion or human review."
        else:
            opening_state = "allowed"
            opening_reason = "All configured opening prerequisites passed."
        self.financial_opening = {
            "state": opening_state,
            "allowed": opening_state == "allowed",
            "criterion_ids": sorted(opening_ids),
            "blocking_criterion_ids": sorted(opening_failures),
            "review_criterion_ids": sorted(opening_review),
            "reason": opening_reason,
        }
        coverage_gaps = list(self.metadata.get("unautomated_requirements") or [])
        if coverage_gaps:
            reason = f"{len(coverage_gaps)} tender requirements remain outside the automated checklist and require human review."
            self.evaluation_completeness.update(state="incomplete", unautomated_requirements=coverage_gaps)
            if self.technical_qualification["state"] != "not_qualified":
                self.technical_qualification.update(state="review_required", reason=reason)
            if self.financial_opening["state"] != "blocked":
                self.financial_opening.update(state="pending", allowed=False, reason=reason)

        self.score = self._score_summary()
        self.preference = self._preference_summary()
        self.post_award = self._post_award_summary()
        return self

    @staticmethod
    def _qualification_summary(
        criteria: Sequence[CriterionDefinition],
        results: Sequence[CriterionResult],
        *,
        label: str,
    ) -> dict[str, Any]:
        failures = [
            criterion.criterion_id
            for criterion, result in zip(criteria, results)
            if result.state in FAILURE_STATES
        ]
        review = [
            criterion.criterion_id
            for criterion, result in zip(criteria, results)
            if result.state not in SUCCESS_STATES | FAILURE_STATES
        ]
        if not criteria:
            state = "not_evaluated"
            reason = f"No {label} gates are configured."
        elif failures:
            state = "not_qualified"
            reason = f"{label.capitalize()} has failed gate evidence."
        elif review:
            state = "review_required"
            reason = f"{label.capitalize()} has unresolved, missing, or incomplete gate evidence."
        else:
            state = "qualified"
            reason = f"All {label} gates passed."
        return _status_summary(
            criteria,
            results,
            state=state,
            blocking=failures,
            review=review,
            reason=reason,
        )

    def _score_summary(self) -> dict[str, Any]:
        criteria, results = _result_groups(
            self.criteria, self.result_by_criterion_id, lambda item: item.criterion_kind == "scored"
        )
        if not criteria:
            return {
                "state": "not_evaluated",
                "criterion_ids": [],
                "total": 0.0,
                "maximum": 0.0,
                "items": {},
            }
        items: dict[str, dict[str, float | None]] = {}
        unresolved: list[str] = []
        total = 0.0
        maximum = 0.0
        has_maximum = True
        for criterion, result in zip(criteria, results):
            if result.state == "not_applicable":
                continue
            if result.score is None:
                unresolved.append(criterion.criterion_id)
            else:
                total += result.score
            max_score = result.maximum_score if result.maximum_score is not None else criterion.max_score
            if max_score is None:
                has_maximum = False
            else:
                maximum += float(max_score)
            items[criterion.criterion_id] = {
                "score": result.score,
                "maximum": None if max_score is None else float(max_score),
            }
            if result.state not in SUCCESS_STATES | FAILURE_STATES:
                unresolved.append(criterion.criterion_id)
        state = "computed" if not unresolved else "review_required"
        return {
            "state": state,
            "criterion_ids": sorted(_ids(criteria)),
            "total": total,
            "maximum": maximum if has_maximum else None,
            "items": {key: items[key] for key in sorted(items)},
            "review_criterion_ids": sorted(set(unresolved)),
        }

    def _preference_summary(self) -> dict[str, Any]:
        criteria, results = _result_groups(
            self.criteria, self.result_by_criterion_id, lambda item: item.criterion_kind == "preference"
        )
        if not criteria or all(result.state == "not_applicable" for result in results):
            state = "not_applicable"
        elif any(result.state in LIMITED_STATES | INCOMPLETE_STATES for result in results):
            state = "review_required"
        elif any(result.state in FAILURE_STATES for result in results):
            state = "ineligible"
        else:
            state = "eligible"
        return {
            "state": state,
            "criterion_ids": sorted(_ids(criteria)),
            "does_not_change_technical_qualification": True,
        }

    def _post_award_summary(self) -> dict[str, Any]:
        criteria, results = _result_groups(
            self.criteria, self.result_by_criterion_id, lambda item: item.criterion_kind == "post_award"
        )
        if not criteria or all(result.state == "not_applicable" for result in results):
            state = "not_applicable"
        elif any(result.state in LIMITED_STATES | INCOMPLETE_STATES for result in results):
            state = "review_required"
        elif any(result.state in FAILURE_STATES for result in results):
            state = "hold"
        else:
            state = "ready"
        return {
            "state": state,
            "criterion_ids": sorted(_ids(criteria)),
            "does_not_block_technical_qualification": True,
            "does_not_block_financial_opening": True,
        }

    def apply_human_review(
        self,
        criterion_id: str,
        *,
        state: State,
        reason: str,
        reviewer_id: str = "",
        evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] | None = None,
        objective_answer: str = "",
        reviewed_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DecisionLedger:
        """Apply a review while retaining the original AI assessment unchanged."""
        result = self.result_for(criterion_id)
        refs = result.effective_assessment.evidence_refs if evidence_refs is None else tuple(evidence_refs)
        review = HumanReview(
            state=state,
            reason=reason,
            reviewer_id=reviewer_id,
            evidence_refs=tuple(EvidenceRef.from_mapping(ref) for ref in refs),
            objective_answer=objective_answer,
            reviewed_at=reviewed_at,
            metadata=dict(metadata or {}),
        )
        effective = result.effective_assessment.with_state(
            review.state,
            reason=review.reason,
            objective_answer=review.objective_answer or result.effective_assessment.objective_answer,
            evidence_refs=review.evidence_refs,
        )
        updated = replace(result, effective_assessment=effective, human_review=review)
        new_results = tuple(updated if item.criterion_id == result.criterion_id else item for item in self.results)
        self.results = new_results
        self._validate_result(
            next(item for item in self.criteria if item.criterion_id == result.criterion_id), updated
        )
        self.recompute()
        return self

    def with_human_review(self, criterion_id: str, **kwargs: Any) -> DecisionLedger:
        """Return a reviewed copy for callers that prefer immutable-style updates."""
        copied = DecisionLedger(
            criteria=self.criteria,
            results=self.results,
            tender_version_id=self.tender_version_id,
            bid_id=self.bid_id,
            schema_version=self.schema_version,
            metadata=dict(self.metadata),
        )
        return copied.apply_human_review(criterion_id, **kwargs)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tender_version_id": self.tender_version_id,
            "bid_id": self.bid_id,
            "criteria": [criterion.to_dict() for criterion in self.criteria],
            "results": [result.to_dict() for result in self.results],
            "derived": {
                "evaluation_completeness": self.evaluation_completeness,
                "technical_qualification": self.technical_qualification,
                "financial_opening": self.financial_opening,
                "score": self.score,
                "preference": self.preference,
                "post_award": self.post_award,
            },
            "metadata": dict(self.metadata),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return self.canonical_payload()

    def compatibility_findings(self) -> list[dict[str, Any]]:
        """Project to the existing finding-list vocabulary without ordinal joins."""
        status_map = {
            "passed": "pass",
            "failed": "fail",
            "missing": "missing",
            "not_applicable": "not_applicable",
            "not_evaluated": "not_evaluated",
            "not_evaluated_after_blocker": "not_evaluated",
            "conflicting": "unclear",
            "needs_human_review": "unclear",
            "search_incomplete": "not_evaluated",
        }
        findings: list[dict[str, Any]] = []
        by_id = self.result_by_criterion_id
        for criterion in self.criteria:
            result = by_id[criterion.criterion_id]
            effective = result.effective_assessment
            refs = effective.evidence_refs
            matched_documents = sorted({ref.document_id for ref in refs})
            missing_documents = list(result.metadata.get("missing_documents", ()))
            findings.append(
                {
                    "criterion_id": criterion.criterion_id,
                    "criterion_text": criterion.requirement,
                    "category": criterion.category,
                    "mandatory": criterion_blocks_qualification(criterion)
                    or criterion_blocks_financial_opening(criterion),
                    "status": status_map[result.state],
                    "reason": effective.reason,
                    "required_documents": list(criterion.evidence_requirements),
                    "missing_documents": missing_documents,
                    "matched_documents": matched_documents,
                    "evidence": [ref.to_dict() for ref in refs],
                }
            )
        return findings

    def compatibility_projection(self) -> dict[str, Any]:
        """Return the legacy report vocabulary; canonical data stays authoritative."""
        technical_state = self.technical_qualification["state"]
        opening_state = self.financial_opening["state"]
        if technical_state == "qualified" and opening_state == "allowed":
            final_decision = "financial_opening_allowed"
            recommendation = "technically_qualified"
        elif technical_state == "not_qualified":
            final_decision = "reject_unresponsive"
            recommendation = "rejected"
        else:
            final_decision = "needs_manual_review"
            recommendation = "needs_manual_review"
        return {
            "findings": self.compatibility_findings(),
            "qualifies": technical_state == "qualified",
            "recommendation": recommendation,
            "final_decision": final_decision,
            "summary": (
                f"{technical_state}; {self.evaluation_completeness['state']} evaluation; "
                f"{len(self.criteria)} active criteria."
            ),
            "financial_opening": {
                "allowed": opening_state == "allowed",
                "status": opening_state,
                "reason": self.financial_opening["reason"],
            },
        }


# Friendly names for callers that prefer the domain terminology.
CanonicalDecisionLedger = DecisionLedger
Criterion = CriterionDefinition
CriterionCell = CriterionResult


def build_decision_ledger(
    criteria: Sequence[CriterionDefinition | Mapping[str, Any]],
    results: Sequence[CriterionResult | Mapping[str, Any]],
    *,
    tender_version_id: str = "",
    bid_id: str = "",
    schema_version: str = "procurement-decision-ledger.v1",
    metadata: Mapping[str, Any] | None = None,
) -> DecisionLedger:
    """Build a complete ledger; missing/extra/duplicate IDs fail closed."""
    return DecisionLedger(
        criteria=tuple(CriterionDefinition.from_mapping(item) for item in criteria),
        results=tuple(CriterionResult.from_mapping(item) for item in results),
        tender_version_id=tender_version_id,
        bid_id=bid_id,
        schema_version=schema_version,
        metadata=dict(metadata or {}),
    )


def canonical_json(value: DecisionLedger | Any) -> str:
    return value.canonical_json() if isinstance(value, DecisionLedger) else _canonical_json(value)


def canonical_hash(value: DecisionLedger | Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compatibility_projection(ledger: DecisionLedger) -> dict[str, Any]:
    return ledger.compatibility_projection()


__all__ = [
    "_ids",
    "_result_groups",
    "_status_summary",
    "DecisionLedger",
    "CanonicalDecisionLedger",
    "Criterion",
    "CriterionCell",
    "build_decision_ledger",
    "canonical_json",
    "canonical_hash",
    "compatibility_projection",
]
