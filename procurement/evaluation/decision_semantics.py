"""Shared decision-ledger states, normalization, and blocking semantics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, TypeAlias


RESULT_STATES = frozenset(
    {
        "passed",
        "failed",
        "missing",
        "conflicting",
        "needs_human_review",
        "not_applicable",
        "not_evaluated",
        "not_evaluated_after_blocker",
        "search_incomplete",
    }
)
CRITERION_KINDS = frozenset(
    {"gate", "scored", "preference", "advisory", "post_award", "internal_gate"}
)
APPLICABILITY_STATES = frozenset({"applicable", "conditional", "not_applicable", "unknown"})
INCOMPLETE_STATES = frozenset({"not_evaluated", "not_evaluated_after_blocker"})
LIMITED_STATES = frozenset({"search_incomplete", "conflicting", "needs_human_review"})
FAILURE_STATES = frozenset({"failed"})
SUCCESS_STATES = frozenset({"passed", "not_applicable"})

State: TypeAlias = str


class DecisionLedgerError(ValueError):
    """Base error for invalid criterion contracts or result ledgers."""


class DuplicateCriterionError(DecisionLedgerError):
    """Raised when a frozen contract or result set repeats an identifier."""


class CriterionSetError(DecisionLedgerError):
    """Raised when results do not exactly cover the active frozen criteria."""


class EvidenceReferenceError(DecisionLedgerError):
    """Raised when an evidence reference is not exact enough to audit."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    raise TypeError(f"expected a mapping or model with model_dump(), got {type(value)!r}")


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _text(value: Any) -> str:
    return str(value or "").strip()


_NON_BLOCKING_MATERIALITIES = frozenset(
    {"advisory", "reference_only", "scored", "preference_only", "post_award"}
)
_NON_BLOCKING_EFFECTS = frozenset(
    {
        "advisory",
        "audit_only",
        "manual_review",
        "no_direct_fail",
        "score",
        "affects_price_ranking",
        "preference_eligibility",
        "contract_remedy",
    }
)


def _semantic_key(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def criterion_has_nonblocking_semantics(
    materiality: Any = None,
    decision_effect: Any = None,
) -> bool:
    """Return whether canonical semantics may not block qualification.

    Legacy producers sometimes leave ``criterion_kind=gate`` in place after
    changing the materiality or decision effect.  The semantic fields are the
    authority at this boundary; the legacy kind is only a fallback.
    """

    return (
        _semantic_key(materiality) in _NON_BLOCKING_MATERIALITIES
        or _semantic_key(decision_effect) in _NON_BLOCKING_EFFECTS
    )


def canonical_criterion_kind(
    criterion_kind: Any,
    materiality: Any = None,
    decision_effect: Any = None,
) -> str:
    """Resolve a criterion kind from its canonical semantics.

    This is deliberately conservative: only a stale gate is downgraded by
    advisory/reference/review/score/preference/post-award semantics.  Internal
    gates retain their separate financial-opening role.
    """

    kind = _semantic_key(criterion_kind) or "gate"
    kind = {
        "internal_gate": "internal_gate",
        "internal_control": "internal_gate",
        "obligation": "post_award",
        "postaward": "post_award",
    }.get(kind, kind)
    materiality_key = _semantic_key(materiality)
    effect_key = _semantic_key(decision_effect)

    if kind == "internal_gate" or materiality_key == "internal_gate":
        return "internal_gate"
    if kind == "post_award" or materiality_key == "post_award" or effect_key == "contract_remedy":
        return "post_award"
    if kind == "preference" or materiality_key == "preference_only" or effect_key == "preference_eligibility":
        return "preference"
    if kind == "scored" or materiality_key in {"scored", "score"} or effect_key == "score":
        return "scored"
    if (
        kind == "advisory"
        or materiality_key in {"advisory", "reference", "reference_only", "review"}
        or effect_key in {"advisory", "audit_only", "manual_review", "no_direct_fail", "affects_price_ranking"}
    ):
        return "advisory"
    return kind


def _tuple_text(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    return tuple(_text(value) for value in values if _text(value))


def _state(value: Any) -> State:
    aliases = {
        "pass": "passed",
        "fail": "failed",
        "unclear": "needs_human_review",
        "review": "needs_human_review",
        "not evaluated": "not_evaluated",
        "not-evaluated": "not_evaluated",
    }
    normalized = aliases.get(_text(value).lower(), _text(value).lower())
    if normalized not in RESULT_STATES:
        raise DecisionLedgerError(f"unsupported criterion result state: {value!r}")
    return normalized


def _json_value(value: Any) -> Any:
    """Return JSON-safe data without relying on a third-party serializer."""
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON serializable: {type(value)!r}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "RESULT_STATES",
    "CRITERION_KINDS",
    "APPLICABILITY_STATES",
    "INCOMPLETE_STATES",
    "LIMITED_STATES",
    "FAILURE_STATES",
    "SUCCESS_STATES",
    "State",
    "DecisionLedgerError",
    "DuplicateCriterionError",
    "CriterionSetError",
    "EvidenceReferenceError",
    "_as_mapping",
    "_first",
    "_text",
    "_NON_BLOCKING_MATERIALITIES",
    "_NON_BLOCKING_EFFECTS",
    "_semantic_key",
    "criterion_has_nonblocking_semantics",
    "canonical_criterion_kind",
    "_tuple_text",
    "_state",
    "_json_value",
    "_canonical_json",
]
