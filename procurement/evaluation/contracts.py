"""Canonical, dependency-free criterion contracts.

This module is intentionally an adapter.  It accepts the dictionaries already
used by the procurement pipeline, keeps their legacy fields, and adds one
stable semantic representation for later evaluators to consume.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterable, Mapping
import hashlib
import json
import re
import unicodedata
from typing import Any

from procurement.evaluation.decision import (
    canonical_criterion_kind,
    criterion_has_nonblocking_semantics,
)


class CriterionContractError(ValueError):
    """Raised when a criterion cannot be made into a coherent contract."""


ContractValidationError = CriterionContractError


_BLOCKING_EFFECTS = {"reject_unresponsive", "block_financial_opening"}
_ALLOWED_KINDS = {
    "gate",
    "scored",
    "preference",
    "obligation",
    "declaration",
    "rollup",
    "internal_control",
    "advisory",
}
_ALLOWED_SCOPES = {
    "bid_submission",
    "technical_qualification",
    "commercial_out_of_scope",
    "post_award",
    "internal_control",
}
_ALLOWED_MATERIALITY = {
    "mandatory_blocking",
    "conditional_blocking",
    "advisory",
    "reference_only",
    "scored",
    "preference_only",
    "post_award",
    "internal_gate",
}
_ALLOWED_APPLICABILITY = {"applicable", "conditional", "not_applicable", "unknown"}
_ALLOWED_EFFECTS = {
    "reject_unresponsive",
    "block_financial_opening",
    "score",
    "affects_price_ranking",
    "preference_eligibility",
    "audit_only",
    "contract_remedy",
    "manual_review",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _display_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _semantic_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _display_text(value)).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json(value[key])
            for key in sorted(value, key=lambda key: str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, str):
        return _display_text(value)
    return deepcopy(value)


def _unique_strings(values: Iterable[Any], *, semantic: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _semantic_text(value) if semantic else _display_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _nested_evaluation(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = _mapping(raw.get("extensions"))
    return _mapping(extensions.get("evaluation"))


def _nested_rule(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the reviewed-checklist rule without making it authoritative twice."""
    return _mapping(raw.get("rule"))


def _enum(value: Any, aliases: Mapping[str, str], default: str) -> str:
    key = _semantic_text(value).replace(" ", "_")
    return aliases.get(key, key if key in aliases.values() else default)


def _criterion_kind(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> str:
    rule = _nested_rule(raw)
    value = _first(
        raw.get("criterion_kind"),
        raw.get("kind"),
        evaluation.get("criterion_kind"),
        evaluation.get("kind"),
        raw.get("requirement_kind"),
        evaluation.get("requirement_kind"),
        rule.get("criterion_kind"),
        rule.get("kind"),
        raw.get("requirement_type"),
        raw.get("priority"),
    )
    aliases = {
        "gate": "gate",
        "mandatory": "gate",
        "requirement": "gate",
        "eligibility": "gate",
        "qualification": "gate",
        "advisory": "advisory",
        "reference": "advisory",
        "reference_only": "advisory",
        "reference-only": "advisory",
        "review": "advisory",
        "scored": "scored",
        "score": "scored",
        "evaluation_scoring": "scored",
        "preference": "preference",
        "mse_preference": "preference",
        "make_in_india_preference": "preference",
        "price_preference": "preference",
        "obligation": "obligation",
        "post_award": "obligation",
        "postaward": "obligation",
        "contractual_obligation": "obligation",
        "declaration": "declaration",
        "form": "declaration",
        "declaratory": "declaration",
        "rollup": "rollup",
        "aggregate": "rollup",
        "summary": "rollup",
        "internal": "internal_control",
        "internal_control": "internal_control",
        "process": "internal_control",
    }
    if value is None:
        materiality = _semantic_text(
            _first(raw.get("materiality"), evaluation.get("materiality"))
        ).replace(" ", "_")
        value = {
            "scored": "scored",
            "preference_only": "preference",
            "post_award": "obligation",
            "internal_gate": "internal_control",
        }.get(materiality)
    kind = _enum(value, aliases, "gate")
    kind = canonical_criterion_kind(
        kind,
        _first(raw.get("materiality"), evaluation.get("materiality")),
        _explicit_effect(raw, evaluation),
    )
    kind = {"post_award": "obligation", "internal_gate": "internal_control"}.get(kind, kind)
    return kind if kind in _ALLOWED_KINDS else "gate"


def _scope(raw: Mapping[str, Any], evaluation: Mapping[str, Any], kind: str) -> str:
    rule = _nested_rule(raw)
    value = _first(
        raw.get("evaluation_scope"),
        raw.get("scope"),
        evaluation.get("evaluation_scope"),
        rule.get("evaluation_scope"),
        raw.get("stage"),
    )
    aliases = {
        "bid": "bid_submission",
        "bid_stage": "bid_submission",
        "bid_submission": "bid_submission",
        "submission": "bid_submission",
        "tender": "bid_submission",
        "technical": "technical_qualification",
        "technical_qualification": "technical_qualification",
        "qualification": "technical_qualification",
        "commercial": "commercial_out_of_scope",
        "commercial_out_of_scope": "commercial_out_of_scope",
        "out_of_scope": "commercial_out_of_scope",
        "post_award": "post_award",
        "postaward": "post_award",
        "delivery_or_inspection": "post_award",
        "internal": "internal_control",
        "internal_control": "internal_control",
    }
    if value is not None:
        normalized = _enum(value, aliases, "")
        if normalized in _ALLOWED_SCOPES:
            return normalized
    if kind == "obligation":
        return "post_award"
    if kind == "internal_control":
        return "internal_control"
    return "bid_submission"


def _applicability(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> dict[str, Any]:
    value = _first(raw.get("applicability"), evaluation.get("applicability"))
    rule = _first(
        raw.get("applicability_rule"),
        evaluation.get("applicability_rule"),
        raw.get("applies_when"),
    )
    conditions = [
        _display_text(condition)
        for condition in _as_list(raw.get("conditions"))
        if _display_text(condition)
    ]
    if isinstance(value, Mapping):
        state = _first(value.get("state"), value.get("status"), value.get("value"))
        rule = _first(value.get("rule"), value.get("condition"), rule)
        conditions.extend(_display_text(item) for item in _as_list(value.get("conditions")) if _display_text(item))
    elif isinstance(value, bool):
        state = "applicable" if value else "not_applicable"
    else:
        state = value
    state_key = _semantic_text(state).replace(" ", "_")
    state_aliases = {
        "found": "applicable",
        "required": "applicable",
        "yes": "applicable",
        "true": "applicable",
        "no": "not_applicable",
        "false": "not_applicable",
        "not_applicable": "not_applicable",
        "n_a": "not_applicable",
        "conditional": "conditional",
        "if_applicable": "conditional",
        "required_if_applicable": "conditional",
        "unknown": "unknown",
        "applicable": "applicable",
    }
    normalized_state = state_aliases.get(state_key)
    if normalized_state is None:
        normalized_state = "conditional" if rule or conditions else "applicable"
    output: dict[str, Any] = {"state": normalized_state}
    if rule:
        output["rule"] = _display_text(rule)
    if conditions:
        output["conditions"] = _unique_strings(conditions)
    return output


def _explicit_effect(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> str | None:
    rule = _nested_rule(raw)
    value = _first(
        raw.get("decision_effect"),
        evaluation.get("decision_effect"),
        raw.get("failure_effect"),
        rule.get("decision_effect"),
        rule.get("failure_effect"),
    )
    if value is None:
        return None
    aliases = {
        "reject": "reject_unresponsive",
        "reject_bid": "reject_unresponsive",
        "fail": "reject_unresponsive",
        "reject_unresponsive": "reject_unresponsive",
        "block": "block_financial_opening",
        "block_financial_opening": "block_financial_opening",
        "score": "score",
        "scoring": "score",
        "price_ranking": "affects_price_ranking",
        "affects_price_ranking": "affects_price_ranking",
        "preference": "preference_eligibility",
        "preference_eligibility": "preference_eligibility",
        "advisory": "audit_only",
        "reference": "audit_only",
        "no_direct_fail": "audit_only",
        "audit": "audit_only",
        "audit_only": "audit_only",
        "post_award": "contract_remedy",
        "contract_remedy": "contract_remedy",
        "review": "manual_review",
        "review_required": "manual_review",
        "manual_review": "manual_review",
    }
    normalized = _enum(value, aliases, "")
    return normalized if normalized in _ALLOWED_EFFECTS else "manual_review"


def _materiality(
    raw: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    kind: str,
    scope: str,
    applicability: Mapping[str, Any],
    explicit_effect: str | None,
) -> str:
    value = _first(raw.get("materiality"), evaluation.get("materiality"))
    aliases = {
        "mandatory": "mandatory_blocking",
        "blocking": "mandatory_blocking",
        "mandatory_blocking": "mandatory_blocking",
        "conditional": "conditional_blocking",
        "conditional_blocking": "conditional_blocking",
        "advisory": "advisory",
        "review": "advisory",
        "reference": "reference_only",
        "reference_only": "reference_only",
        "reference-only": "reference_only",
        "scored": "scored",
        "score": "scored",
        "preference": "preference_only",
        "preference_only": "preference_only",
        "post_award": "post_award",
        "internal": "internal_gate",
        "internal_gate": "internal_gate",
    }
    if value is not None:
        normalized = _enum(value, aliases, "advisory")
        if normalized in _ALLOWED_MATERIALITY:
            return normalized
    if scope == "post_award":
        return "post_award"
    if scope == "internal_control" or kind == "internal_control":
        return "internal_gate"
    if kind == "scored":
        return "scored"
    if kind == "preference":
        return "preference_only"
    if applicability.get("state") == "conditional":
        return "conditional_blocking"
    if raw.get("mandatory") is True or explicit_effect in _BLOCKING_EFFECTS:
        return "mandatory_blocking"
    if kind in {"gate", "declaration"} and raw.get("mandatory") is not False:
        return "mandatory_blocking"
    return "advisory"


def _decision_effect(
    explicit_effect: str | None,
    kind: str,
    scope: str,
    materiality: str,
    applicability: Mapping[str, Any],
) -> str:
    if explicit_effect is not None:
        return explicit_effect
    if applicability.get("state") == "not_applicable":
        return "audit_only"
    if kind == "scored" or materiality == "scored":
        return "score"
    if kind == "preference" or materiality == "preference_only":
        return "preference_eligibility"
    if scope == "post_award" or materiality == "post_award":
        return "contract_remedy"
    if scope == "commercial_out_of_scope":
        return "audit_only"
    if kind == "internal_control":
        return "manual_review"
    if materiality in {"mandatory_blocking", "conditional_blocking", "internal_gate"}:
        return "reject_unresponsive"
    return "manual_review"


def _threshold(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> dict[str, Any]:
    rule = _nested_rule(raw)
    value = _first(
        raw.get("threshold"),
        raw.get("thresholds"),
        evaluation.get("threshold"),
        evaluation.get("thresholds"),
        rule.get("threshold"),
        rule.get("thresholds"),
    )
    if value is not None:
        if isinstance(value, Mapping):
            return _normalize_json(value)
        return {"value": _normalize_json(value)}
    separate = {
        key: raw[key]
        for key in ("operator", "value", "minimum", "maximum", "unit", "period", "currency", "basis")
        if key in raw and raw[key] is not None
    }
    return _normalize_json(separate)


def _source_refs(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for candidate in (
        raw.get("source_refs"),
        raw.get("source_references"),
        raw.get("rfp_source_refs"),
        raw.get("sources"),
        evaluation.get("source_refs"),
        evaluation.get("source_references"),
        _mapping(raw.get("extensions")).get("provenance") and _mapping(_mapping(raw.get("extensions")).get("provenance")).get("source_references"),
    ):
        values.extend(_as_list(candidate))
    if not values and _first(raw.get("source_document_id"), raw.get("source_page_id"), raw.get("source_page_number")) is not None:
        values.append({
            "document_id": raw.get("source_document_id"),
            "page_id": raw.get("source_page_id"),
            "page_number": raw.get("source_page_number"),
            "clause_no": raw.get("source_clause_no"),
            "quote": raw.get("source_quote"),
        })
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            ref = _normalize_json(value)
        else:
            ref = {"quote": _display_text(value)}
        if not any(item not in (None, "", [], {}) for item in ref.values()):
            continue
        key = json.dumps(ref, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            refs.append(ref)
    return refs


def _evidence_spec(value: Any, evidence_ids: set[str] | None = None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = _normalize_json(value)
        if not result.get("document_name") and result.get("name"):
            result["document_name"] = result["name"]
        if not result.get("document_type") and result.get("type"):
            result["document_type"] = result["type"]
        return result
    text = _display_text(value)
    return {"evidence_id": text} if evidence_ids and text in evidence_ids else {"document_name": text}


def _evidence_groups(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups = _first(raw.get("evidence_groups"), evaluation.get("evidence_groups"))
    evidence_ids = {
        _display_text(item.get("evidence_id"))
        for item in _as_list(raw.get("required_evidence"))
        if isinstance(item, Mapping) and _display_text(item.get("evidence_id"))
    }
    output: list[dict[str, Any]] = []
    if groups:
        for group in _as_list(groups):
            if isinstance(group, Mapping):
                result = _normalize_json(group)
                mode = _semantic_text(_first(group.get("mode"), group.get("operator"), "AND")).upper()
                result["mode"] = mode if mode in {"AND", "OR"} else "AND"
                entries = _first(group.get("evidence"), group.get("items"), group.get("required_evidence"))
                if entries is not None:
                    result["evidence"] = [_evidence_spec(item, evidence_ids) for item in _as_list(entries)]
                output.append(result)
            elif isinstance(group, (list, tuple, set)):
                output.append({
                    "mode": "OR",
                    "evidence": [_evidence_spec(item, evidence_ids) for item in group],
                })
            else:
                output.append({"mode": "OR", "evidence": [_evidence_spec(group, evidence_ids)]})
        return output
    evidence = _first(
        raw.get("required_evidence"),
        raw.get("evidence_requirements"),
        raw.get("required_documents"),
        _nested_rule(raw).get("required_documents"),
    )
    if evidence:
        return [{
            "mode": "AND",
            "evidence": [_evidence_spec(item) for item in _as_list(evidence)],
        }]
    return []


def _id_list(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidates = _as_list(value)
        if isinstance(value, Mapping):
            candidates = [value]
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                candidate = _first(candidate.get("criterion_id"), candidate.get("id"), candidate.get("check_id"))
            text = _display_text(candidate)
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                result.append(text)
    return sorted(result, key=str.casefold)


def _dependencies(raw: Mapping[str, Any], evaluation: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    dependencies = raw.get("dependencies")
    if isinstance(dependencies, Mapping):
        required = _first(raw.get("depends_on"), dependencies.get("requires"), dependencies.get("depends_on"))
        blocks = _first(raw.get("blocks"), dependencies.get("blocks"))
    else:
        required = _first(raw.get("depends_on"), dependencies, evaluation.get("depends_on"))
        blocks = _first(raw.get("blocks"), evaluation.get("blocks"))
    return _id_list(required), _id_list(blocks)


def _semantic_group(group: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(group))
    for key in ("evidence_ids", "id", "source_refs", "source_references", "provenance"):
        result.pop(key, None)
    if "evidence" in result:
        result["evidence"] = [
            _semantic_evidence(item)
            for item in result["evidence"]
        ]
    return _normalize_json(result)


def _semantic_evidence(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return {"document_name": _semantic_text(value)}
    result = {}
    for key, item in value.items():
        if key in {"evidence_id", "id", "source_refs", "source_references", "provenance"}:
            continue
        if isinstance(item, str):
            result[str(key)] = _semantic_text(item)
        elif isinstance(item, list):
            result[str(key)] = sorted(_semantic_text(entry) for entry in item)
        else:
            result[str(key)] = _normalize_json(item)
    return result


def _semantic_payload(criterion: Mapping[str, Any]) -> dict[str, Any]:
    applicability = _mapping(criterion.get("applicability"))
    return {
        "semantic_key": _semantic_text(criterion.get("semantic_key")),
        "requirement": _semantic_text(criterion.get("requirement")),
        "category": _semantic_text(criterion.get("category")),
        "criterion_kind": criterion.get("criterion_kind"),
        "evaluation_scope": criterion.get("evaluation_scope"),
        "materiality": criterion.get("materiality"),
        "applicability": _normalize_json(applicability),
        "decision_effect": criterion.get("decision_effect"),
        "threshold": _normalize_json(criterion.get("threshold") or {}),
        "pass_rule": _semantic_text(criterion.get("pass_rule")),
        "evidence_groups": sorted(
            (_semantic_group(group) for group in criterion.get("evidence_groups") or []),
            key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False),
        ),
        "depends_on": sorted(criterion.get("depends_on") or [], key=str.casefold),
        "blocks": sorted(criterion.get("blocks") or [], key=str.casefold),
    }


def _fingerprint_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_normalized(criterion: Mapping[str, Any], raw: Mapping[str, Any] | None = None) -> None:
    criterion_id = _display_text(criterion.get("criterion_id"))
    if not criterion_id:
        raise CriterionContractError("criterion_id is required")
    kind = criterion.get("criterion_kind")
    scope = criterion.get("evaluation_scope")
    materiality = criterion.get("materiality")
    applicability = _mapping(criterion.get("applicability"))
    effect = criterion.get("decision_effect")
    if kind not in _ALLOWED_KINDS:
        raise CriterionContractError(f"{criterion_id}: unsupported criterion_kind {kind!r}")
    if scope not in _ALLOWED_SCOPES:
        raise CriterionContractError(f"{criterion_id}: unsupported evaluation_scope {scope!r}")
    if materiality not in _ALLOWED_MATERIALITY:
        raise CriterionContractError(f"{criterion_id}: unsupported materiality {materiality!r}")
    if applicability.get("state") not in _ALLOWED_APPLICABILITY:
        raise CriterionContractError(f"{criterion_id}: unsupported applicability state")
    if effect not in _ALLOWED_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: unsupported decision_effect {effect!r}")
    if applicability.get("state") == "conditional" and not (
        applicability.get("rule") or applicability.get("conditions")
    ):
        raise CriterionContractError(f"{criterion_id}: conditional applicability needs a rule or condition")
    if scope == "post_award" and effect in _BLOCKING_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: post-award criteria cannot block bid qualification")
    if kind == "preference" and effect in _BLOCKING_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: preference criteria cannot reject a bid")
    if kind == "scored" and effect in _BLOCKING_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: scored criteria cannot reject without a separate gate")
    if materiality in {"advisory", "reference_only", "scored", "preference_only", "post_award"} and effect in _BLOCKING_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: {materiality} cannot use a blocking effect")
    if scope == "commercial_out_of_scope" and effect in _BLOCKING_EFFECTS:
        raise CriterionContractError(f"{criterion_id}: commercial out-of-scope criteria cannot block")
    if raw is not None:
        if (
            raw.get("mandatory") is True
            and not criterion.get("mandatory")
            and _semantic_text(materiality).replace(" ", "_")
            not in {"advisory", "reference_only"}
            and _semantic_text(effect).replace(" ", "_")
            not in {"manual_review", "audit_only", "no_direct_fail"}
        ):
            raise CriterionContractError(f"{criterion_id}: legacy mandatory conflicts with canonical semantics")
        if (
            raw.get("mandatory") is False
            and criterion.get("mandatory")
            and materiality != "conditional_blocking"
            and applicability.get("state") != "conditional"
        ):
            raise CriterionContractError(f"{criterion_id}: legacy non-mandatory conflicts with canonical semantics")
        raw_effect = _explicit_effect(raw, _nested_evaluation(raw))
        if raw_effect in _BLOCKING_EFFECTS and effect not in _BLOCKING_EFFECTS:
            raise CriterionContractError(f"{criterion_id}: legacy failure_effect conflicts with canonical semantics")
    if criterion_id.casefold() in {item.casefold() for item in criterion.get("depends_on") or []}:
        raise CriterionContractError(f"{criterion_id}: criterion cannot depend on itself")


def normalize_criterion(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return one canonical criterion while retaining legacy input fields."""
    if not isinstance(raw, Mapping):
        raise CriterionContractError("criterion must be a mapping")
    evaluation = _nested_evaluation(raw)
    rule = _nested_rule(raw)
    criterion_id = _first(raw.get("criterion_id"), raw.get("check_id"), raw.get("requirement_id"), raw.get("id"))
    if not _display_text(criterion_id):
        raise CriterionContractError("criterion_id is required")
    kind = _criterion_kind(raw, evaluation)
    scope = _scope(raw, evaluation, kind)
    applicability = _applicability(raw, evaluation)
    explicit_effect = _explicit_effect(raw, evaluation)
    materiality = _materiality(raw, evaluation, kind, scope, applicability, explicit_effect)
    effect = _decision_effect(explicit_effect, kind, scope, materiality, applicability)
    mandatory = materiality in {"mandatory_blocking", "conditional_blocking", "internal_gate"}
    if criterion_has_nonblocking_semantics(materiality, effect):
        mandatory = False
    if applicability.get("state") == "not_applicable":
        mandatory = False
    if scope in {"post_award", "commercial_out_of_scope"} or kind in {"scored", "preference"}:
        mandatory = False
    failure_effect = effect if effect in _BLOCKING_EFFECTS else "manual_review"
    pass_rule = _display_text(
        _first(
            raw.get("pass_rule"),
            raw.get("pass_condition"),
            raw.get("acceptance_rule"),
            rule.get("pass_rule"),
            "all_required_evidence_must_pass",
        )
    )
    threshold = _threshold(raw, evaluation)
    groups = _evidence_groups(raw, evaluation)
    depends_on, blocks = _dependencies(raw, evaluation)
    source_refs = _source_refs(raw, evaluation)
    semantic_key = _first(
        raw.get("semantic_key"),
        evaluation.get("semantic_key"),
        raw.get("field_key"),
    )
    if semantic_key is None:
        semantic_key = f"generic.{_semantic_text(_first(raw.get('requirement'), raw.get('title'), criterion_id)).replace(' ', '_')}"
    semantic_key = _display_text(semantic_key).casefold()
    result = deepcopy(dict(raw))
    result["criterion_id"] = _display_text(criterion_id)
    result.setdefault(
        "requirement",
        _display_text(_first(raw.get("requirement"), rule.get("requirement"), raw.get("title"), criterion_id)),
    )
    result.setdefault("category", _display_text(_first(raw.get("category"), raw.get("family"), rule.get("rule_family"), "other")))
    result.setdefault("stage", _display_text(raw.get("stage") or scope))
    result["criterion_kind"] = kind
    result["evaluation_scope"] = scope
    result["materiality"] = materiality
    result["applicability"] = applicability
    result["decision_effect"] = effect
    result["threshold"] = threshold
    result["evidence_groups"] = groups
    result["depends_on"] = depends_on
    result["blocks"] = blocks
    result["semantic_key"] = semantic_key
    result["mandatory"] = mandatory
    result["failure_effect"] = failure_effect
    result["pass_rule"] = pass_rule
    result.setdefault("pass_condition", pass_rule)
    result["source_refs"] = source_refs
    result["source_references"] = deepcopy(source_refs)
    if not result.get("source_quote") and source_refs:
        result["source_quote"] = _first(source_refs[0].get("quote"), source_refs[0].get("text"))
    result["aliases"] = []
    result["merged_from"] = [result["criterion_id"]]
    result["semantic_fingerprint"] = _fingerprint_payload(_semantic_payload(result))
    extensions = deepcopy(_mapping(result.get("extensions")))
    normalized_evaluation = deepcopy(_mapping(extensions.get("evaluation")))
    for key, value in {
        "criterion_kind": kind,
        "evaluation_scope": scope,
        "materiality": materiality,
        "applicability": deepcopy(applicability),
        "decision_effect": effect,
        "semantic_key": semantic_key,
        "depends_on": list(depends_on),
        "blocks": list(blocks),
        "threshold": deepcopy(threshold),
    }.items():
        normalized_evaluation.setdefault(key, value)
    extensions["evaluation"] = normalized_evaluation
    canonical_metadata = deepcopy(_mapping(extensions.get("canonical_contract")))
    canonical_metadata.update({
        "version": "canonical-criterion.v1",
        "semantic_fingerprint": result["semantic_fingerprint"],
        "evidence_groups": deepcopy(groups),
        "criterion_kind": kind,
        "evaluation_scope": scope,
        "materiality": materiality,
        "applicability": deepcopy(applicability),
        "decision_effect": effect,
        "semantic_key": semantic_key,
        "depends_on": list(depends_on),
        "blocks": list(blocks),
        "threshold": deepcopy(threshold),
    })
    extensions["canonical_contract"] = canonical_metadata
    result["extensions"] = extensions
    _validate_normalized(result, raw)
    return result


def validate_criterion(criterion: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate one criterion, raising on contradictions."""
    return normalize_criterion(criterion)


def validate_criteria(criteria: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a list and reject empty, duplicate, or contradictory criteria."""
    normalized = [normalize_criterion(item) for item in criteria]
    seen: set[str] = set()
    for item in normalized:
        key = item["criterion_id"].casefold()
        if key in seen:
            raise CriterionContractError(f"duplicate criterion_id: {item['criterion_id']}")
        seen.add(key)
    return normalized


def _merge_unique_records(first: list[Any], second: list[Any]) -> list[Any]:
    result = deepcopy(first)
    seen = {
        json.dumps(_normalize_json(item), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        for item in result
    }
    for item in second:
        key = json.dumps(_normalize_json(item), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(item))
    return result


def _merge_equivalent(first: dict[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(first)
    primary_id = merged["criterion_id"]
    merged_from = _unique_strings(
        [*(merged.get("merged_from") or [primary_id]), *(second.get("merged_from") or [second.get("criterion_id")])]
    )
    merged["merged_from"] = merged_from
    merged["aliases"] = [item for item in merged_from if item != primary_id]
    merged["source_refs"] = _merge_unique_records(merged.get("source_refs") or [], second.get("source_refs") or [])
    merged["source_references"] = deepcopy(merged["source_refs"])
    merged["evidence_groups"] = _merge_unique_records(merged.get("evidence_groups") or [], second.get("evidence_groups") or [])
    merged["depends_on"] = _id_list(merged.get("depends_on"), second.get("depends_on"))
    merged["blocks"] = _id_list(merged.get("blocks"), second.get("blocks"))
    if not merged.get("source_quote"):
        merged["source_quote"] = second.get("source_quote")
    if not merged.get("required_evidence") and second.get("required_evidence"):
        merged["required_evidence"] = deepcopy(second["required_evidence"])
    elif second.get("required_evidence"):
        merged["required_evidence"] = _merge_unique_records(
            merged.get("required_evidence") or [], second.get("required_evidence") or []
        )
    extensions = deepcopy(_mapping(merged.get("extensions")))
    metadata = deepcopy(_mapping(extensions.get("canonical_contract")))
    metadata["aliases"] = list(merged["aliases"])
    metadata["merged_from"] = list(merged["merged_from"])
    extensions["canonical_contract"] = metadata
    merged["extensions"] = extensions
    merged["semantic_fingerprint"] = _fingerprint_payload(_semantic_payload(merged))
    metadata["semantic_fingerprint"] = merged["semantic_fingerprint"]
    return merged


def deduplicate_criteria(criteria: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge only semantically identical criteria, retaining IDs and sources."""
    normalized = validate_criteria(criteria)
    buckets: dict[str, list[dict[str, Any]]] = {}
    output: list[dict[str, Any]] = []
    for item in normalized:
        fingerprint = semantic_fingerprint(item)
        payload = _semantic_payload(item)
        match = None
        for candidate in buckets.setdefault(fingerprint, []):
            if _semantic_payload(candidate) == payload:
                match = candidate
                break
        if match is None:
            buckets[fingerprint].append(item)
            output.append(item)
        else:
            merged = _merge_equivalent(match, item)
            index = output.index(match)
            output[index] = merged
            bucket_index = buckets[fingerprint].index(match)
            buckets[fingerprint][bucket_index] = merged
    validate_criteria(output)
    return output


def normalize_contract(
    criteria: Iterable[Mapping[str, Any]],
    *,
    deduplicate: bool = False,
) -> list[dict[str, Any]]:
    """Normalize a criterion set, optionally applying safe semantic dedupe."""
    return deduplicate_criteria(criteria) if deduplicate else validate_criteria(criteria)


def semantic_fingerprint(criterion: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint of decision semantics only."""
    normalized = criterion if "criterion_kind" in criterion else normalize_criterion(criterion)
    return _fingerprint_payload(_semantic_payload(normalized))


__all__ = [
    "ContractValidationError",
    "CriterionContractError",
    "deduplicate_criteria",
    "normalize_contract",
    "normalize_criterion",
    "semantic_fingerprint",
    "validate_criteria",
    "validate_criterion",
]
