"""Deterministic projections applied after field-result normalization."""

from __future__ import annotations

import json
import re
from typing import Any

from .field_core import (
    _EXCEPTION_CUES,
    _compact_text,
    _contains_phrase,
    _phrase_text,
)
from .schema import RfpFieldSpec


def _project_controlling_scalar(
    field: RfpFieldSpec,
    result: dict[str, Any],
    effective_state: dict[str, Any],
) -> dict[str, Any]:
    if result.get("status") != "found" or field.value_type not in {"date", "datetime"}:
        return result
    operations = [
        operation
        for operation in effective_state.get("operations") or []
        if operation.get("operation") in {"add", "replace", "clarify"}
        and _compact_text(operation.get("effective_text"))
    ]
    if len(operations) == 1:
        result["value"] = _compact_text(operations[0]["effective_text"])
    return result


def _project_deleted_members(
    result: dict[str, Any],
    effective_state: dict[str, Any],
) -> dict[str, Any]:
    values = result.get("value")
    if result.get("status") != "found" or not isinstance(values, list):
        return result
    targets = [
        _compact_text(operation.get("target"))
        for operation in effective_state.get("operations") or []
        if operation.get("operation") == "delete"
        and _compact_text(operation.get("target"))
    ]
    if not targets:
        return result
    retained = [
        value
        for value in values
        if not any(
            _contains_phrase(_phrase_text(str(value)), target)
            or _contains_phrase(_phrase_text(target), value)
            for target in targets
        )
    ]
    if retained and len(retained) != len(values):
        result["value"] = retained
        result["exceptions"] = list(
            dict.fromkeys(
                [
                    *(result.get("exceptions") or []),
                    *[f"Deleted by controlling amendment: {target}" for target in targets],
                ]
            )
        )[:12]
    return result


def _project_template_conflict(
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
) -> dict[str, Any]:
    value = result.get("value")
    if result.get("status") != "found" or not isinstance(value, str):
        return result
    current_ids = set(effective_state.get("current_source_ids") or [])
    current = [
        item
        for item in evidence
        if not current_ids or item.get("source_id") in current_ids
    ]
    has_electronic_instruction = any(
        "electronic mode only" in _phrase_text(str(item.get("text") or ""))
        and any(
            cue in _phrase_text(" ".join(item.get("section_path") or []))
            for cue in ("submission", "instruction", "late bids")
        )
        for item in current
    )
    has_envelope_template = any(
        "sealed" in _phrase_text(str(item.get("text") or ""))
        and "envelope" in _phrase_text(str(item.get("text") or ""))
        and any(
            cue in _phrase_text(" ".join(item.get("section_path") or []))
            for cue in ("form", "cover letter", "template", "annexure")
        )
        for item in current
    )
    if not (has_electronic_instruction and has_envelope_template):
        return result
    sentences = re.split(r"(?<=[.!?])\s+", _compact_text(value))
    conflicting = [
        sentence
        for sentence in sentences
        if "sealed" in sentence.casefold() and "envelope" in sentence.casefold()
    ]
    retained = [sentence for sentence in sentences if sentence not in conflicting]
    if conflicting and retained:
        result["value"] = " ".join(retained)
        result["exceptions"] = list(
            dict.fromkeys(
                [
                    *(result.get("exceptions") or []),
                    "A covering-letter template mentions sealed separate envelopes, but the dedicated electronic-only submission instructions control.",
                ]
            )
        )[:12]
    return result


def _project_explicit_formulas(
    field: RfpFieldSpec,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
) -> dict[str, Any]:
    if result.get("status") != "found" or "formula" not in _phrase_text(field.prompt):
        return result
    current_ids = set(effective_state.get("current_source_ids") or [])
    formulas: dict[str, str] = {}
    for item in evidence:
        if current_ids and item.get("source_id") not in current_ids:
            continue
        text = _compact_text(item.get("text"))
        for variable in re.findall(r"\b([A-Za-z][A-Za-z0-9]{0,3})\s*=", text):
            formulas.setdefault(variable.casefold(), text[:500])
    if not formulas:
        return result

    def without_false_absence(value: Any) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", _compact_text(value))
        return " ".join(
            sentence
            for sentence in sentences
            if not (
                "formula" in sentence.casefold()
                and any(cue in sentence.casefold() for cue in ("not provided", "is missing"))
            )
        )

    current_value = result.get("value")
    if isinstance(current_value, list):
        result["value"] = [
            cleaned
            for item in current_value
            if (cleaned := without_false_absence(item))
        ]
    else:
        result["value"] = without_false_absence(current_value)
    raw_answer = json.dumps(
        {"value": result.get("value"), "conditions": result.get("conditions") or []},
        ensure_ascii=True,
    )
    answer = _phrase_text(raw_answer)
    missing = [
        formula
        for variable, formula in formulas.items()
        if not re.search(
            rf"\b{re.escape(variable)}\b.{{0,40}}(?:equals|calculated as)",
            answer,
            re.I,
        )
        and not re.search(rf"\b{re.escape(variable)}\s*=", raw_answer, re.I)
    ]
    result["conditions"] = list(
        dict.fromkeys([*(result.get("conditions") or []), *missing])
    )[:12]
    return result


def _project_prompt_exclusions(
    field: RfpFieldSpec,
    result: dict[str, Any],
) -> dict[str, Any]:
    prompt = _phrase_text(field.prompt)
    if "exclude" not in prompt:
        return result
    excluded = [
        cue
        for cue in ("consortium", "joint venture", "subcontracting", "sub contracting")
        if cue in prompt
    ]
    if excluded:
        result["exceptions"] = [
            item
            for item in result.get("exceptions") or []
            if not any(cue in _phrase_text(str(item)) for cue in excluded)
        ]
    return result


def _project_currency_markers(
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    source_text = " ".join(str(item.get("text") or "") for item in evidence)
    if not re.search(
        r"(?:\u20b9|\u00e2\u201a\u00b9|\u00e2\u20ac\u00b9|rs\.?|\binr\b)",
        source_text,
        re.I,
    ):
        return result

    def normalize(value: Any) -> Any:
        if isinstance(value, str):
            value = re.sub(
                r"(?:\u20b9|\u00e2\u201a\u00b9|\u00e2\u20ac\u00b9|\x08)\s*(?=\d)",
                "INR ",
                value,
            )
            if re.search(r"\b(?:lakh|lakhs|crore|crores|turnover|emd)\b", value, re.I):
                value = re.sub(r"\s*\n+\s*(?=\d)", " INR ", value)
                value = re.sub(r'(?<!\w)"\s*,?\s*(?=\d)', "INR ", value)
                value = re.sub(r"\bLakhe\b", "Lakh", value, flags=re.I)
                value = re.sub(r"\bLakh\s+Crore\b", "Lakh", value, flags=re.I)
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    for key in ("value", "conditions", "exceptions", "required_documents"):
        result[key] = normalize(result.get(key))
    return result


def _explicit_total_mark_values(text: str) -> set[str]:
    return set(
        re.findall(
            r"\b(?:grand\s+total|total\s*(?:/\s*vendor)?\s*(?:marks?|score)|"
            r"total\s+vendor\s+score)\s*[:\-]?\s*(\d+(?:\.\d+)?)\b",
            text,
            re.I,
        )
    )


def _project_controlling_total_marks(
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
) -> dict[str, Any]:
    if result.get("status") != "found":
        return result
    controlling_ids = set(effective_state.get("controlling_source_ids") or [])
    controlling_text = " ".join(
        str(item.get("text") or "")
        for item in evidence
        if item.get("source_id") in controlling_ids
    )
    current_ids = set(effective_state.get("current_source_ids") or [])
    current_evidence = [
        item
        for item in evidence
        if not current_ids or item.get("source_id") in current_ids
    ]
    current_text = " ".join(str(item.get("text") or "") for item in current_evidence)
    totals = _explicit_total_mark_values(
        " ".join(part for part in (controlling_text, current_text) if part)
    )
    if len(totals) != 1:
        return result
    total = next(iter(totals))
    answer_text = json.dumps(
        {"value": result.get("value"), "conditions": result.get("conditions") or []},
        ensure_ascii=True,
    )
    if re.search(
        rf"(?:\b{re.escape(total)}\s*marks?\b|\b(?:grand\s+total|total\s*(?:/\s*vendor)?\s*(?:marks?|score))\s*[:\-]?\s*{re.escape(total)}\b)",
        answer_text,
        re.I,
    ):
        return result
    display_total = total.rstrip("0").rstrip(".") if "." in total else total
    result["conditions"] = [
        *[
            condition
            for condition in result.get("conditions") or []
            if not re.match(
                r"^\s*(?:grand\s+total|total\s+(?:maximum\s+)?(?:marks?|score))\s*[:\-]",
                str(condition),
                re.I,
            )
        ],
        f"Total score: {display_total} marks",
    ]
    matching_evidence = next(
        (
            item
            for item in current_evidence
            if total in _explicit_total_mark_values(str(item.get("text") or ""))
        ),
        None,
    )
    if matching_evidence and matching_evidence.get("source_id"):
        hydrated = {
            "source_id": str(matching_evidence.get("source_id") or ""),
            "document_id": str(matching_evidence.get("document_id") or matching_evidence.get("document") or ""),
            "page": matching_evidence.get("page"),
            "page_id": str(matching_evidence.get("page_id") or ""),
            "source_file": str(matching_evidence.get("source_file") or ""),
            "source_role": str(matching_evidence.get("source_role") or "unknown"),
            "quote": str(matching_evidence.get("text") or "")[:2500],
        }
        existing_ids = {
            str(item.get("source_id") or "")
            for item in result.get("evidence") or []
        }
        if hydrated["source_id"] not in existing_ids:
            result["evidence"] = [*(result.get("evidence") or []), hydrated]
    return result


__all__ = [
    "_project_controlling_scalar",
    "_project_deleted_members",
    "_project_template_conflict",
    "_project_explicit_formulas",
    "_project_prompt_exclusions",
    "_project_currency_markers",
    "_explicit_total_mark_values",
    "_project_controlling_total_marks",
]
