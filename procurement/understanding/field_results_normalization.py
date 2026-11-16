"""Field-result normalization and response-shape cleanup."""

from __future__ import annotations

import re
from typing import Any

from .field_core import (
    _EXCEPTION_CUES,
    _clean_list,
    _compact_text,
    _explicit_boolean_polarity,
    _is_placeholder_value,
    _phrase_text,
    _unit_text,
    _value_matches,
)
from .field_results_evidence import (
    _date_value_grounded,
    _explicit_not_applicable_value,
    _hydrate_evidence,
    _money_value_grounded,
)
from .schema import RfpFieldSpec


def _normalize_result(
    raw: dict[str, Any],
    field: RfpFieldSpec,
    source_units: dict[str, dict[str, Any]],
    valid_page_ids: set[str],
    allowed_source_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    status = str(raw.get("status") or "unresolved")
    if status not in {
        "found",
        "not_found",
        "not_applicable",
        "ambiguous",
        "need_more_evidence",
    }:
        status = "unresolved"
        errors.append("invalid status")

    source_ids = [
        source_id
        for source_id in _clean_list(raw.get("evidence_source_ids"))
        if source_id in source_units
        and (allowed_source_ids is None or source_id in allowed_source_ids)
    ]
    unknown_source_ids = [
        source_id
        for source_id in _clean_list(raw.get("evidence_source_ids"))
        if source_id not in source_units
        or (allowed_source_ids is not None and source_id not in allowed_source_ids)
    ]
    page_ids = [
        page_id
        for page_id in _clean_list(raw.get("evidence_page_ids"))
        if page_id in valid_page_ids
    ]
    unknown_page_ids = [
        page_id
        for page_id in _clean_list(raw.get("evidence_page_ids"))
        if page_id not in valid_page_ids
    ]
    if unknown_source_ids:
        errors.append(f"unknown evidence_source_ids: {unknown_source_ids[:5]}")
    if unknown_page_ids:
        errors.append(f"unknown evidence_page_ids: {unknown_page_ids[:5]}")

    value = raw.get("value")
    if isinstance(value, str):
        value = _compact_text(value)
    if (
        status == "found"
        and _phrase_text(str(value or "")) in {"na", "n a", "not applicable", "nil", "not required"}
        and source_ids
        and _explicit_not_applicable_value(field, source_ids, source_units)
    ):
        status = "not_applicable"
        value = None
    if status != "found":
        value = None
    if status == "found" and not _value_matches(value, field.value_type):
        errors.append(f"value does not match {field.value_type}")
    if status == "found" and _is_placeholder_value(value):
        errors.append("value is a template placeholder")
    if status == "found" and field.validation.require_evidence and not (source_ids or page_ids):
        errors.append("found result has no valid source evidence")
    if (
        status == "found"
        and field.value_type == "money"
        and source_ids
        and not _money_value_grounded(value, source_ids, source_units, field)
    ):
        errors.append("money value is not present in cited evidence")
    if (
        status == "found"
        and field.value_type in {"date", "datetime"}
        and source_ids
        and not _date_value_grounded(value, source_ids, source_units)
    ):
        errors.append("date value is not present in cited evidence")
    if (
        status == "found"
        and field.value_type == "boolean"
        and value is False
        and source_ids
        and not any(
            _explicit_boolean_polarity(field, _unit_text(source_units[source_id])) is False
            for source_id in source_ids
        )
    ):
        errors.append("negative boolean value is not explicit in cited evidence")

    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
        errors.append("invalid confidence")

    result = {
        "key": field.key,
        "label": field.label,
        "status": status,
        "value": value if status == "found" else None,
        "conditions": _clean_list(raw.get("conditions")),
        "exceptions": _clean_list(raw.get("exceptions")),
        "required_documents": _clean_list(raw.get("required_documents")),
        "evidence": _hydrate_evidence(source_ids, page_ids, source_units),
        "confidence": confidence,
        "notes": _compact_text(raw.get("notes"))[:1000],
    }
    return result, errors


def _serialized_collection_artifact(value: str) -> bool:
    compact = _compact_text(value)
    return bool(
        compact.endswith("]")
        and (
            compact.startswith("[")
            or "', '" in compact
            or '\", \"' in compact
        )
    )


def _drop_serialized_collection_artifacts(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("conditions", "exceptions", "required_documents"):
        result[key] = [
            value
            for value in _clean_list(result.get(key), limit=16)
            if not _serialized_collection_artifact(value)
        ]
    return result


def _promote_explicit_exceptions(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "found" or result.get("exceptions"):
        return result
    raw_value = result.get("value")
    values = [
        *([] if isinstance(raw_value, list) else [str(raw_value or "")]),
        *[str(item) for item in result.get("conditions") or []],
    ]
    exception_sentences = [
        _compact_text(sentence)
        for value in values
        for sentence in re.split(r"(?<=[.;])\s+", value)
        if any(cue in sentence.casefold() for cue in _EXCEPTION_CUES)
    ]
    if exception_sentences:
        result["exceptions"] = list(dict.fromkeys(exception_sentences))[:12]
    return result


__all__ = [
    "_normalize_result",
    "_serialized_collection_artifact",
    "_drop_serialized_collection_artifacts",
    "_promote_explicit_exceptions",
]
