"""BHEL baseline and tender-derived requirement-matrix construction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from collections.abc import Mapping, Callable

from .bhel_evaluation_common import (
    _BASELINE_SEMANTIC_KEYS,
    _BLOCKING,
    _EVALUATION_SCOPES,
    _ID_FIELDS,
    _mapping,
    _mapping_list,
    _model_data,
)
from .bhel_evaluation_profile import _applicability_rule_allows, _evaluation_scope, load_bhel_profile
from .compliance.profile import PURCHASE_POLICY, policy_id_for_profile
from .bhel_evaluation_sources import (
    _explicit_sources,
    _matching_references,
    _normalize_source_references,
    _source_grounded,
    _source_index,
)


def _candidate_items(value: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    value = _model_data(value)
    if isinstance(value, Mapping):
        for key in keys:
            if key not in value:
                continue
            raw = value[key]
            if isinstance(raw, Mapping):
                if any(name in raw for name in _ID_FIELDS) or any(name in raw for name in ("status", "result", "requirement")):
                    candidates.append(deepcopy(dict(raw)))
                else:
                    for child_key, child in raw.items():
                        if isinstance(child, Mapping):
                            item = deepcopy(dict(child))
                            item.setdefault("criterion_id", str(child_key))
                            candidates.append(item)
            elif isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, Mapping):
                        continue
                    candidate = deepcopy(dict(item))
                    # RequirementMatrix.items is produced from the reviewed
                    # tender ledger and must never disappear. This marker
                    # does not claim provenance; missing citations remain visible.
                    if key == "items":
                        candidate["_frozen_matrix_item"] = True
                    candidates.append(candidate)
    return candidates


def _dynamic_items(matrix: Mapping[str, Any], understanding: Mapping[str, Any]) -> list[dict[str, Any]]:
    keys = ("items", "criteria", "qualification_checks", "tender_derived_checks", "dynamic_checks", "requirements")
    items = _candidate_items(matrix, keys) + _candidate_items(understanding, keys)
    contract = _mapping(understanding.get("frozen_contract"))
    items += _candidate_items(contract, ("criteria", "requirements"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        identifier = str(
            item.get("criterion_id")
            or item.get("check_id")
            or item.get("id")
            or item.get("key")
            or item.get("title")
            or item.get("label")
            or ""
        ).strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        item["criterion_id"] = identifier
        result.append(item)
    return result


def _bhel_extension(matrix: Mapping[str, Any]) -> dict[str, Any]:
    extensions = _mapping(matrix.get("extensions"))
    return _mapping(extensions.get("bhel")) or _mapping(matrix.get("bhel"))


def _criterion_id(check: Mapping[str, Any]) -> str:
    identifier = str(check.get("check_id") or check.get("criterion_id") or "").strip()
    return identifier if identifier.startswith("BHEL-") else f"BHEL-{identifier}"


def _source_quote(check: Mapping[str, Any]) -> str | None:
    for key in ("source_quote", "quote"):
        value = check.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for reference in check.get("source_references") or []:
        if isinstance(reference, Mapping):
            quote = reference.get("quote")
            if isinstance(quote, str) and quote.strip():
                return quote.strip()
    return None


def _required_evidence(check: Mapping[str, Any]) -> list[dict[str, Any]]:
    existing = check.get("required_evidence")
    if isinstance(existing, list) and existing:
        return [deepcopy(dict(item)) for item in existing if isinstance(item, Mapping)]
    check_id = str(check.get("check_id") or check.get("criterion_id") or "check")
    label = str(check.get("label") or check.get("requirement") or check_id)
    blocking = check.get("materiality") in _BLOCKING
    return [{
        "evidence_id": f"BHEL-{check_id}-proof",
        "document_name": label,
        "document_type": "bidder_submission",
        "required": blocking,
        "checks": [check_id],
        "acceptance_rule": "Evidence must be present in the frozen bidder submission and satisfy the tender requirement.",
        "missing_effect": "reject" if blocking else "review",
    }]


def _requirement_item(check: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    check_id = str(check.get("check_id") or check.get("criterion_id") or "")
    scope = _evaluation_scope(check) or "technical_qualification"
    blocking = check.get("materiality") in _BLOCKING
    metadata = {
        "check_id": check_id,
        "profile_id": profile.get("profile_id", "bhel_purchase_materials"),
        "source_row": check.get("source_row"),
        "origin": check.get("origin") or "bhel_baseline",
        "applicability": "applicable",
        "evaluation_scope": scope,
        "materiality": check.get("materiality") or "mandatory_blocking",
        "source_references": _normalize_source_references(check.get("source_references") or []),
        "submission_policy": deepcopy(profile.get("submission_policy") or {}),
    }
    return {
        "criterion_id": _criterion_id(check),
        "requirement": check.get("requirement") or check.get("label") or check_id,
        "category": check.get("category") or "other",
        "stage": check.get("stage") or "bid_submission",
        "mandatory": blocking,
        "clarifiable": False,
        "failure_effect": check.get("failure_effect") or ("reject_unresponsive" if blocking else "review_required"),
        "pass_rule": check.get("pass_rule") or "all_required_evidence_must_pass",
        "source_quote": _source_quote(check),
        "required_evidence": _required_evidence(check),
        "extensions": {"bhel": metadata},
    }


def _append_bhel_items(merged: dict[str, Any], extension: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    items = merged.get("items")
    if not isinstance(items, list):
        items = []
        merged["items"] = items
    existing_ids = {
        str(_mapping(item).get("criterion_id") or "").casefold()
        for item in items
        if _mapping(item).get("criterion_id")
    }
    existing_semantics: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        evaluation = extensions.get("evaluation") if isinstance(extensions, Mapping) else None
        semantic_key = str(evaluation.get("semantic_key") or "") if isinstance(evaluation, Mapping) else ""
        if semantic_key:
            existing_semantics.setdefault(semantic_key, item)
    candidates = [
        *_mapping_list(extension.get("bidder_checks")),
        *_mapping_list(extension.get("dynamic_checks")),
    ]
    for check in candidates:
        if check.get("applicability") != "applicable":
            continue
        if _evaluation_scope(check) != "technical_qualification":
            continue
        check_id = str(check.get("check_id") or check.get("criterion_id") or "")
        if not check_id or check.get("kind") == "internal_control":
            continue
        if check_id.casefold().startswith("field-"):
            continue
        criterion_id = _criterion_id(check)
        if criterion_id.casefold() in existing_ids or check_id.casefold() in existing_ids:
            continue
        semantic_key = _BASELINE_SEMANTIC_KEYS.get(check_id)
        if semantic_key and semantic_key in existing_semantics:
            target = existing_semantics[semantic_key]
            target_extensions = target.setdefault("extensions", {})
            merged_bhel = target_extensions.setdefault("bhel", {})
            merged_ids = merged_bhel.setdefault("merged_baseline_check_ids", [])
            if check_id not in merged_ids:
                merged_ids.append(check_id)
            merged_bhel["origin"] = "tender_derived+bhel_baseline"
            merged_bhel["semantic_key"] = semantic_key
            merged_bhel["evaluation_scope"] = _evaluation_scope(check) or "technical_qualification"
            merged_bhel["materiality"] = check.get("materiality") or "conditional_blocking"
            merged_bhel["applicability"] = check.get("applicability") or "applicable"
            merged_bhel["source_references"] = _normalize_source_references(check.get("source_references") or [])
            continue
        items.append(_requirement_item(check, profile))
        existing_ids.add(criterion_id.casefold())


def merge_bhel_profile(
    matrix: dict,
    understanding: dict,
    units: dict,
    *,
    load_profile: Callable[[], dict | None] = load_bhel_profile,
) -> dict:
    """Add a BHEL extension without changing the caller's matrix in place."""
    merged = deepcopy(_model_data(matrix)) if isinstance(_model_data(matrix), dict) else {}
    understanding = _mapping(understanding)
    units = _mapping(units)
    profile = load_profile()
    if profile is None:
        return merged
    tender_profile = _mapping(understanding.get("tender_profile"))
    if (
        profile.get("profile_id") == "bhel_purchase_materials"
        and policy_id_for_profile(tender_profile) != PURCHASE_POLICY
    ):
        return merged

    sources = _source_index(understanding, units)
    baseline: list[dict[str, Any]] = []
    for original in profile.get("baseline_checks", []):
        row = deepcopy(original)
        scope = _evaluation_scope(row)
        references = _matching_references(row, sources)
        active = (
            bool(references)
            and scope in _EVALUATION_SCOPES
            and _applicability_rule_allows(row, understanding)
        )
        if scope:
            row["evaluation_scope"] = scope
        row["applicability"] = "applicable" if active else "not_applicable"
        row["source_references"] = references
        row["result"] = "not_evaluated" if active else "not_applicable"
        baseline.append(row)

    dynamic: list[dict[str, Any]] = []
    dynamic_internal_controls: list[dict[str, Any]] = []
    dynamic_commercial: list[dict[str, Any]] = []
    baseline_aliases: set[str] = set()
    for row in baseline:
        baseline_aliases.update(_id_aliases(row.get("check_id")))
    for candidate in _dynamic_items(_mapping(matrix), _mapping(understanding)):
        identifier = str(candidate.get("criterion_id") or "")
        if _id_aliases(identifier) & baseline_aliases:
            continue
        grounded, references = _source_grounded(candidate, sources)
        frozen_matrix_item = candidate.get("_frozen_matrix_item") is True
        scope = _evaluation_scope(candidate) or "commercial_out_of_scope"
        extensions = candidate.get("extensions")
        evaluation = extensions.get("evaluation") if isinstance(extensions, Mapping) else None
        evaluation = evaluation if isinstance(evaluation, Mapping) else {}
        declared_applicability = str(
            candidate.get("applicability")
            or evaluation.get("applicability")
            or "applicable"
        ).strip().casefold()
        applicable = declared_applicability != "not_applicable" and (grounded or frozen_matrix_item)
        dynamic_check = {
            "check_id": identifier,
            "criterion_id": identifier,
            "label": candidate.get("title") or candidate.get("label") or candidate.get("name") or identifier,
            "requirement": candidate.get("requirement") or candidate.get("description") or candidate.get("title") or identifier,
            "category": candidate.get("category") or "tender_derived",
            "kind": "bidder_check" if scope == "technical_qualification" else scope,
            "evaluation_scope": scope,
            "origin": "tender_derived",
            "materiality": candidate.get("materiality") or evaluation.get("materiality") or (
                "mandatory_blocking" if candidate.get("mandatory", True) else "advisory"
            ),
            "applicability": declared_applicability if applicable else "not_applicable",
            "source_grounded": grounded,
            "provenance_status": "verified" if grounded else ("missing" if frozen_matrix_item else "not_applicable"),
            "source_references": references,
            "source_quote": candidate.get("source_quote"),
            "required_evidence": deepcopy(candidate.get("required_evidence") or []),
            "required_proof": deepcopy(candidate.get("required_proof") or []),
            "pass_rule": candidate.get("pass_rule"),
            "failure_effect": candidate.get("failure_effect"),
            "result": (
                "not_evaluated"
                if applicable and grounded
                else ("needs_review" if applicable and frozen_matrix_item else "not_applicable")
            ),
        }
        if scope == "internal_control":
            dynamic_internal_controls.append(dynamic_check)
        elif scope == "commercial_out_of_scope":
            dynamic_commercial.append(dynamic_check)
        else:
            dynamic.append(dynamic_check)

    extension = {
        "schema_version": profile.get("schema_version", "bhel-bid-evaluation.v1"),
        "profile_id": profile.get("profile_id", "bhel_purchase_materials"),
        "source": deepcopy(profile.get("source") or {}),
        "submission_policy": deepcopy(profile.get("submission_policy") or {}),
        "result_statuses": list(profile.get("result_statuses") or RESULT_STATUSES),
        "baseline_checks": baseline,
        "bidder_checks": [
            deepcopy(row)
            for row in baseline
            if _evaluation_scope(row) == "technical_qualification"
        ],
        "internal_controls": [
            *[
                deepcopy(row)
                for row in baseline
                if _evaluation_scope(row) == "internal_control"
            ],
            *dynamic_internal_controls,
        ],
        "commercial_out_of_scope": [
            *[
                deepcopy(row)
                for row in baseline
                if _evaluation_scope(row) == "commercial_out_of_scope"
            ],
            *dynamic_commercial,
        ],
        "dynamic_checks": dynamic,
    }
    extensions = merged.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        merged["extensions"] = extensions
    extensions["bhel"] = extension
    _append_bhel_items(merged, extension, profile)
    return merged


def _id_aliases(identifier: Any) -> set[str]:
    value = str(identifier or "").strip()
    if not value:
        return set()
    bare = value[5:] if value.casefold().startswith("bhel-") else value
    return {
        value,
        value.casefold(),
        bare,
        bare.casefold(),
        f"BHEL-{bare}",
        f"bhel-{bare.casefold()}",
    }
