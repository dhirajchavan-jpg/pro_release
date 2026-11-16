"""Deterministic field-result derivation from effective amendment state."""

from __future__ import annotations

import re
from typing import Any

from .field_core import (
    _EXCEPTION_CUES,
    _NUMBER_WORDS,
    _clean_list,
    _compact_text,
    _configured_anchor_match,
    _contains_phrase,
    _direct_field_match,
    _explicit_boolean_polarity,
    _numeric_token_compatible_for_field,
    _phrase_text,
    _requires_exhaustive_coverage,
    _signal_phrase_covered,
    _tokens,
    _unit_page,
    _unit_text,
)
from .field_results_evidence import _hydrate_evidence
from .schema import RfpFieldSpec


def _deterministic_boolean_result(
    field: RfpFieldSpec,
    effective_state: dict[str, Any],
    source_units: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, bool, str, list[str]]] = []
    operations = [
        operation
        for operation in effective_state.get("operations") or []
        if operation.get("operation") in {"add", "replace", "clarify", "retain"}
    ]
    for index, operation in enumerate(operations):
        text = _compact_text(operation.get("effective_text"))
        polarity = _explicit_boolean_polarity(field, text)
        if polarity is not None:
            candidates.append(
                (
                    int(operation.get("authority_rank") or 0),
                    int(operation.get("source_order") or index),
                    polarity,
                    text,
                    [str(value) for value in operation.get("source_ids") or []],
                )
            )
    if not candidates:
        source_ids = list(
            effective_state.get("controlling_source_ids")
            or effective_state.get("current_source_ids")
            or []
        )
        for index, source_id in enumerate(source_ids):
            unit = source_units.get(str(source_id)) or {}
            searchable = _phrase_text(_unit_text(unit))
            if not (
                _direct_field_match(unit, field)
                or _configured_anchor_match(unit, field)
                or any(
                    _signal_phrase_covered(searchable, phrase)
                    for phrase in (field.label, *field.retrieval_guidance.aliases)
                )
            ):
                continue
            text = _compact_text(_unit_text(unit))
            polarity = _explicit_boolean_polarity(field, text)
            if polarity is not None:
                candidates.append(
                    (
                        int(unit.get("authority_rank") or 0),
                        int(unit.get("document_order") or 0) * 10_000
                        + _unit_page(unit) * 100
                        + index,
                        polarity,
                        text,
                        [str(source_id)],
                    )
                )
    if not candidates:
        return None
    latest_rank = max(candidate[:2] for candidate in candidates)
    latest = [candidate for candidate in candidates if candidate[:2] == latest_rank]
    polarities = {candidate[2] for candidate in latest}
    if len(polarities) != 1:
        return None
    _authority, _order, value, text, source_ids = latest[-1]
    required_documents: list[str] = []
    if value and "submitted" in _phrase_text(text):
        searchable = _phrase_text(
            " ".join(
                [
                    text,
                    *[
                        _unit_text(source_units.get(source_id) or {})
                        for source_id in source_ids
                    ],
                ]
            )
        )
        document_name = next(
            (
                _compact_text(phrase).title()
                for phrase in (
                    *field.retrieval_guidance.aliases,
                    *field.retrieval_guidance.anchor_phrases,
                )
                if len(_tokens(str(phrase))) >= 2
                and _contains_phrase(searchable, phrase)
            ),
            "",
        )
        annexure_match = re.search(
            r"\bannexure\s+([ivxlcdm]+)\b.{0,40}\bintegrity\s+pact\b",
            searchable,
            re.I,
        )
        if annexure_match:
            document_name = (
                f"Annexure {annexure_match.group(1).upper()} - Integrity Pact"
            )
        if document_name:
            required_documents.append(document_name)
    return {
        "key": field.key,
        "label": field.label,
        "status": "found",
        "value": value,
        "conditions": [text[:300]] if value else [],
        "exceptions": [],
        "required_documents": required_documents,
        "evidence": _hydrate_evidence(source_ids, [], source_units),
        "confidence": 0.99,
        "notes": "",
    }


def _deterministic_scalar_result(
    field: RfpFieldSpec,
    effective_state: dict[str, Any],
    source_units: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    package_status = effective_state.get("package_amendment_status")
    all_operations = list(effective_state.get("operations") or [])
    if (
        not _requires_exhaustive_coverage(field)
        and package_status == "resolved"
        and all_operations
        and all(
        operation.get("operation") == "delete" for operation in all_operations
        )
    ):
        source_ids = list(
            dict.fromkeys(
                str(source_id)
                for operation in all_operations
                for source_id in operation.get("source_ids") or []
            )
        )
        return {
            "key": field.key,
            "label": field.label,
            "status": "not_found",
            "value": None,
            "conditions": [],
            "exceptions": ["The controlling amendment deletes this requirement."],
            "required_documents": [],
            "evidence": _hydrate_evidence(source_ids, [], source_units),
            "confidence": 1.0,
            "notes": "",
        }
    if field.value_type == "boolean" and package_status in {"resolved", "no_change"}:
        boolean_result = _deterministic_boolean_result(
            field, effective_state, source_units
        )
        if boolean_result is not None:
            return boolean_result
    if package_status != "resolved":
        return None
    operations = [
        operation
        for operation in effective_state.get("operations") or []
        if operation.get("operation") in {"add", "replace", "clarify", "retain"}
        and _compact_text(operation.get("effective_text"))
    ]
    qualified_scalar_value: int | float | None = None
    qualified_scalar_operation: dict[str, Any] | None = None
    if field.value_type in {"number", "integer"}:
        qualified_by_operation: list[tuple[dict[str, Any], int]] = []
        for candidate in operations:
            candidate_text = _compact_text(candidate.get("effective_text"))
            for match in re.finditer(
                r"\b(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s+"
                r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b",
                candidate_text.casefold(),
            ):
                if field.value_type == "integer" and re.match(
                    r"\s*%", candidate_text[match.end() :]
                ):
                    continue
                if not _numeric_token_compatible_for_field(
                    field, candidate_text, match.start(1), match.end(1)
                ):
                    continue
                raw_value = match.group(1)
                value = _NUMBER_WORDS.get(
                    raw_value,
                    int(raw_value) if raw_value.isdigit() else -1,
                )
                if value >= 0:
                    qualified_by_operation.append((candidate, value))
        qualified_values = {value for _candidate, value in qualified_by_operation}
        if len(qualified_values) == 1:
            qualified_scalar_value = next(iter(qualified_values))
            qualified_scalar_operation = max(
                (
                    candidate
                    for candidate, value in qualified_by_operation
                    if value == qualified_scalar_value
                ),
                key=lambda candidate: (
                    int(candidate.get("authority_rank") or 0),
                    int(candidate.get("source_order") or 0),
                ),
            )

    replacements = [
        operation for operation in operations if operation.get("operation") == "replace"
    ]
    if qualified_scalar_operation is not None:
        operation = qualified_scalar_operation
    elif len(replacements) == 1:
        operation = replacements[0]
    elif len(operations) == 1:
        operation = operations[0]
    else:
        return None
    effective_text = _compact_text(operation["effective_text"])
    value: Any
    if field.value_type in {"date", "datetime"}:
        value = effective_text
    elif field.value_type == "money":
        amounts = re.findall(
            r"(?<![A-Za-z])(?:INR|Rs\.?|₹)\s*[\d,.]+"
            r"(?:\s*(?:crores?|lakhs?|lacs?|million|billion))?",
            effective_text,
            re.I,
        )
        normalized_amounts = list(dict.fromkeys(_compact_text(amount) for amount in amounts))
        if len(normalized_amounts) != 1:
            return None
        value = normalized_amounts[0]
    elif field.value_type in {"number", "integer"}:
        numbers = (
            [str(qualified_scalar_value)]
            if qualified_scalar_value is not None
            else list(
                dict.fromkeys(
                    match.group(0)
                    for match in re.finditer(
                        r"(?<![a-z])\d+(?:\.\d+)?", effective_text.casefold()
                    )
                    if _numeric_token_compatible_for_field(
                        field, effective_text, match.start(), match.end()
                    )
                )
            )
        )
        if len(numbers) != 1:
            return None
        value = int(numbers[0]) if field.value_type == "integer" else float(numbers[0])
        if field.value_type == "number" and value.is_integer():
            value = int(value)
    elif field.value_type == "string":
        if (
            len(operations) != 1
            or _requires_exhaustive_coverage(field)
            or operation.get("operation") != "replace"
        ):
            return None
        value = effective_text
    else:
        return None
    exception_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.;])\s+", effective_text)
        if any(cue in sentence.casefold() for cue in _EXCEPTION_CUES)
    ]
    source_ids = list(
        dict.fromkeys(
            str(source_id)
            for supporting_operation in operations
            for source_id in supporting_operation.get("source_ids") or []
            if str(source_id) in source_units
        )
    )
    supporting_conditions = list(
        dict.fromkeys(
            _compact_text(supporting_operation.get("effective_text"))
            for supporting_operation in operations
            if _compact_text(supporting_operation.get("effective_text"))
        )
    )
    required_documents = list(
        dict.fromkeys(
            document
            for supporting_operation in operations
            for document in _clean_list(
                supporting_operation.get("required_documents"), limit=16
            )
        )
    )
    return {
        "key": field.key,
        "label": field.label,
        "status": "found",
        "value": value,
        "conditions": (
            []
            if field.value_type in {"date", "datetime", "string"}
            else supporting_conditions[:16]
        ),
        "exceptions": exception_sentences,
        "required_documents": (
            []
            if field.value_type in {"date", "datetime"}
            else required_documents[:16]
        ),
        "evidence": _hydrate_evidence(source_ids, [], source_units),
        "confidence": min(1.0, max(0.99, float(operation.get("confidence") or 0.0))),
        "notes": "",
    }


__all__ = ["_deterministic_boolean_result", "_deterministic_scalar_result"]
