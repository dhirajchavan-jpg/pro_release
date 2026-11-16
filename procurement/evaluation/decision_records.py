"""Canonical evidence, assessment, criterion, and result records."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Mapping, Sequence
from typing import Any

from .decision_semantics import (
    APPLICABILITY_STATES,
    CRITERION_KINDS,
    DecisionLedgerError,
    EvidenceReferenceError,
    State,
    _as_mapping,
    _first,
    _state,
    _text,
    _tuple_text,
    canonical_criterion_kind,
)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """An auditable, exact source location.

    A page alone is not enough.  The reference must identify the source page and
    at least one exact locator: text quote, block/table cell, image crop, or
    another stable locator recorded by the extractor.
    """

    document_id: str
    page: int | None = None
    page_id: str | None = None
    quote: str | None = None
    block_id: str | None = None
    table_cell: str | None = None
    image_ref: str | None = None
    bbox: tuple[float, ...] | None = None
    content_hash: str | None = None
    locator: str | None = None
    file_name: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.document_id):
            raise EvidenceReferenceError("evidence.document_id is required")
        if self.page is not None and (isinstance(self.page, bool) or self.page < 1):
            raise EvidenceReferenceError("evidence.page must be a positive integer")
        if not _text(self.page_id) and self.page is None:
            raise EvidenceReferenceError("evidence requires page or page_id")
        locators = (self.quote, self.block_id, self.table_cell, self.image_ref, self.locator)
        if not any(_text(item) for item in locators) and not self.bbox:
            raise EvidenceReferenceError(
                "evidence requires quote, block/table locator, image_ref, locator, or bbox"
            )
        if self.bbox is not None:
            object.__setattr__(self, "bbox", tuple(float(item) for item in self.bbox))

    @classmethod
    def from_mapping(cls, value: EvidenceRef | Mapping[str, Any]) -> EvidenceRef:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        bbox = _first(data, "bbox", "bounding_box")
        return cls(
            document_id=_text(_first(data, "document_id", "document", "source_document_id")),
            page=_first(data, "page", "page_number"),
            page_id=_first(data, "page_id", "source_page_id"),
            quote=_first(data, "quote", "text_span", "span"),
            block_id=_first(data, "block_id", "source_block_id"),
            table_cell=_first(data, "table_cell", "cell_id", "table_id"),
            image_ref=_first(data, "image_ref", "image_crop", "crop_ref"),
            bbox=tuple(bbox) if bbox else None,
            content_hash=_first(data, "content_hash", "page_hash"),
            locator=_first(data, "locator", "anchor"),
            file_name=_first(data, "file_name", "document_name"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"document_id": self.document_id}
        for name in (
            "page",
            "page_id",
            "quote",
            "block_id",
            "table_cell",
            "image_ref",
            "content_hash",
            "locator",
            "file_name",
        ):
            value = getattr(self, name)
            if value not in (None, ""):
                result[name] = value
        if self.bbox:
            result["bbox"] = list(self.bbox)
        return result


@dataclass(frozen=True, slots=True)
class Assessment:
    """One machine or human assessment, including its own evidence."""

    state: State
    objective_answer: str = ""
    reason: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    confidence: float | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _state(self.state))
        refs = tuple(EvidenceRef.from_mapping(ref) for ref in self.evidence_refs)
        object.__setattr__(self, "evidence_refs", refs)
        if self.confidence is not None and not 0 <= float(self.confidence) <= 1:
            raise DecisionLedgerError("assessment.confidence must be between 0 and 1")

    @classmethod
    def from_mapping(cls, value: Assessment | Mapping[str, Any]) -> Assessment:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        refs = _first(data, "evidence_refs", "evidence", "references", default=())
        return cls(
            state=_first(data, "state", "status", "decision", default="needs_human_review"),
            objective_answer=_text(_first(data, "objective_answer", "answer")),
            reason=_text(_first(data, "reason", "explanation")),
            evidence_refs=tuple(refs or ()),
            confidence=_first(data, "confidence"),
            facts=dict(_first(data, "facts", "observed_facts", default={}) or {}),
            metadata=dict(_first(data, "metadata", default={}) or {}),
        )

    def with_state(
        self,
        state: State,
        *,
        reason: str | None = None,
        objective_answer: str | None = None,
        evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] | None = None,
    ) -> Assessment:
        return replace(
            self,
            state=_state(state),
            reason=self.reason if reason is None else _text(reason),
            objective_answer=(
                self.objective_answer if objective_answer is None else _text(objective_answer)
            ),
            evidence_refs=(
                self.evidence_refs
                if evidence_refs is None
                else tuple(EvidenceRef.from_mapping(ref) for ref in evidence_refs)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "objective_answer": self.objective_answer,
            "reason": self.reason,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "confidence": self.confidence,
            "facts": dict(self.facts),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HumanReview:
    """The append-only human correction metadata for one criterion."""

    state: State
    reason: str
    reviewer_id: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    objective_answer: str = ""
    reviewed_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _state(self.state))
        if not _text(self.reason):
            raise DecisionLedgerError("human review reason is required")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(EvidenceRef.from_mapping(ref) for ref in self.evidence_refs),
        )

    @classmethod
    def from_mapping(cls, value: HumanReview | Mapping[str, Any]) -> HumanReview:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        refs = _first(data, "evidence_refs", "evidence", "references", default=())
        return cls(
            state=_first(data, "state", "status", "decision"),
            reason=_text(_first(data, "reason", "note", "explanation")),
            reviewer_id=_text(_first(data, "reviewer_id", "reviewer", "actor")),
            evidence_refs=tuple(refs or ()),
            objective_answer=_text(_first(data, "objective_answer", "answer")),
            reviewed_at=_first(data, "reviewed_at", "timestamp"),
            metadata=dict(_first(data, "metadata", default={}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "reviewer_id": self.reviewer_id,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "objective_answer": self.objective_answer,
            "reviewed_at": self.reviewed_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CriterionDefinition:
    """The immutable semantic contract for one frozen criterion."""

    criterion_id: str
    requirement: str
    category: str = "other"
    criterion_kind: str = "gate"
    evaluation_scope: str = "technical_qualification"
    materiality: str | None = None
    decision_effect: str | None = None
    applicability: str = "applicable"
    active: bool = True
    threshold: Mapping[str, Any] = field(default_factory=dict)
    pass_rule: str = ""
    evidence_requirements: tuple[str, ...] = ()
    source_references: tuple[EvidenceRef, ...] = ()
    depends_on: tuple[str, ...] = ()
    max_score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        criterion_id = _text(self.criterion_id)
        if not criterion_id:
            raise DecisionLedgerError("criterion_id is required")
        materiality = _text(self.materiality) if self.materiality is not None else None
        decision_effect = _text(self.decision_effect) if self.decision_effect is not None else None
        criterion_kind = canonical_criterion_kind(
            self.criterion_kind,
            materiality,
            decision_effect,
        )
        if criterion_kind not in CRITERION_KINDS:
            raise DecisionLedgerError(f"unsupported criterion_kind: {self.criterion_kind!r}")
        if materiality is None:
            materiality = {
                "gate": "mandatory_blocking",
                "scored": "scored",
                "preference": "preference_only",
                "advisory": "advisory",
                "post_award": "post_award",
                "internal_gate": "internal_gate",
            }[criterion_kind]
        if decision_effect is None:
            decision_effect = {
                "gate": "reject_unresponsive",
                "scored": "score",
                "preference": "preference_eligibility",
                "advisory": "audit_only",
                "post_award": "contract_remedy",
                "internal_gate": "block_financial_opening",
            }[criterion_kind]
        if self.applicability not in APPLICABILITY_STATES:
            raise DecisionLedgerError(f"unsupported applicability: {self.applicability!r}")
        object.__setattr__(self, "criterion_id", criterion_id)
        object.__setattr__(self, "requirement", _text(self.requirement))
        object.__setattr__(self, "criterion_kind", criterion_kind)
        object.__setattr__(self, "materiality", materiality)
        object.__setattr__(self, "decision_effect", decision_effect)
        object.__setattr__(self, "evidence_requirements", _tuple_text(self.evidence_requirements))
        object.__setattr__(
            self,
            "source_references",
            tuple(EvidenceRef.from_mapping(ref) for ref in self.source_references),
        )
        object.__setattr__(self, "depends_on", _tuple_text(self.depends_on))
        if self.max_score is not None and float(self.max_score) < 0:
            raise DecisionLedgerError("max_score cannot be negative")

    @property
    def kind(self) -> str:
        return self.criterion_kind

    @classmethod
    def from_mapping(cls, value: CriterionDefinition | Mapping[str, Any]) -> CriterionDefinition:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        kind = _text(_first(data, "criterion_kind", "kind", "type", default="gate")).lower()
        if kind not in CRITERION_KINDS:
            aliases = {
                "internal-gate": "internal_gate",
                "post-award": "post_award",
                "reference": "advisory",
                "reference-only": "advisory",
            }
            kind = aliases.get(kind, kind)
        scope = _text(
            _first(data, "evaluation_scope", "scope", default="technical_qualification")
        )
        materiality = _first(data, "materiality")
        effect = _first(data, "decision_effect", "effect", "failure_effect")
        kind = canonical_criterion_kind(kind, materiality, effect)
        if materiality is None:
            materiality = {
                "gate": "mandatory_blocking",
                "scored": "scored",
                "preference": "preference_only",
                "advisory": "advisory",
                "post_award": "post_award",
                "internal_gate": "internal_gate",
            }[kind]
        if effect is None:
            effect = {
                "gate": "reject_unresponsive",
                "scored": "score",
                "preference": "preference_eligibility",
                "advisory": "audit_only",
                "post_award": "contract_remedy",
                "internal_gate": "block_financial_opening",
            }[kind]
        kind = canonical_criterion_kind(kind, materiality, effect)
        return cls(
            criterion_id=_text(_first(data, "criterion_id", "id")),
            requirement=_text(_first(data, "requirement", "criterion_text", "text")),
            category=_text(_first(data, "category", default="other")),
            criterion_kind=kind,
            evaluation_scope=scope,
            materiality=_text(materiality),
            decision_effect=_text(effect),
            applicability=_text(_first(data, "applicability", default="applicable")),
            active=bool(_first(data, "active", default=True)),
            threshold=dict(_first(data, "threshold", default={}) or {}),
            pass_rule=_text(_first(data, "pass_rule", "pass_condition", default="")),
            evidence_requirements=tuple(
                _first(data, "evidence_requirements", "required_documents", "proof", default=())
                or ()
            ),
            source_references=tuple(
                _first(data, "source_references", "source_refs", "evidence", default=()) or ()
            ),
            depends_on=tuple(_first(data, "depends_on", "dependencies", default=()) or ()),
            max_score=_first(data, "max_score", "maximum_score", "maximum"),
            metadata=dict(_first(data, "metadata", "extensions", default={}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "requirement": self.requirement,
            "category": self.category,
            "criterion_kind": self.criterion_kind,
            "evaluation_scope": self.evaluation_scope,
            "materiality": self.materiality,
            "decision_effect": self.decision_effect,
            "applicability": self.applicability,
            "active": self.active,
            "threshold": dict(self.threshold),
            "pass_rule": self.pass_rule,
            "evidence_requirements": list(self.evidence_requirements),
            "source_references": [ref.to_dict() for ref in self.source_references],
            "depends_on": list(self.depends_on),
            "max_score": self.max_score,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """The immutable machine result plus its effective reviewed result."""

    criterion_id: str
    ai_assessment: Assessment
    effective_assessment: Assessment | None = None
    human_review: HumanReview | None = None
    score: float | None = None
    maximum_score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "criterion_id", _text(self.criterion_id))
        if not self.criterion_id:
            raise DecisionLedgerError("result.criterion_id is required")
        ai = Assessment.from_mapping(self.ai_assessment)
        object.__setattr__(self, "ai_assessment", ai)
        effective = self.effective_assessment
        if effective is None:
            effective = ai
        else:
            effective = Assessment.from_mapping(effective)
        review = self.human_review
        if review is not None:
            review = HumanReview.from_mapping(review)
            if self.effective_assessment is None:
                effective = ai.with_state(
                    review.state,
                    reason=review.reason,
                    objective_answer=review.objective_answer or ai.objective_answer,
                    evidence_refs=review.evidence_refs or ai.evidence_refs,
                )
        object.__setattr__(self, "effective_assessment", effective)
        object.__setattr__(self, "human_review", review)
        if self.score is not None:
            object.__setattr__(self, "score", float(self.score))
        if self.maximum_score is not None:
            object.__setattr__(self, "maximum_score", float(self.maximum_score))

    @property
    def state(self) -> State:
        assert self.effective_assessment is not None
        return self.effective_assessment.state

    @classmethod
    def from_mapping(cls, value: CriterionResult | Mapping[str, Any]) -> CriterionResult:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        ai_data = _first(data, "ai_assessment", "ai", "machine_assessment")
        if ai_data is None:
            ai_data = data
        score_value = _first(data, "score")
        if isinstance(score_value, Mapping):
            score_value = _first(score_value, "value", "score")
        return cls(
            criterion_id=_text(_first(data, "criterion_id", "id")),
            ai_assessment=Assessment.from_mapping(ai_data),
            effective_assessment=(
                Assessment.from_mapping(_first(data, "effective_assessment", "effective"))
                if _first(data, "effective_assessment", "effective") is not None
                else None
            ),
            human_review=(
                HumanReview.from_mapping(_first(data, "human_review", "review"))
                if _first(data, "human_review", "review") is not None
                else None
            ),
            score=score_value,
            maximum_score=_first(data, "maximum_score", "max_score"),
            metadata=dict(_first(data, "metadata", "extensions", default={}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "ai_assessment": self.ai_assessment.to_dict(),
            "effective_assessment": self.effective_assessment.to_dict()
            if self.effective_assessment
            else None,
            "human_review": self.human_review.to_dict() if self.human_review else None,
            "score": self.score,
            "maximum_score": self.maximum_score,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "EvidenceRef",
    "Assessment",
    "HumanReview",
    "CriterionDefinition",
    "CriterionResult",
]
