"""Configuration and profile loading for the BHEL evaluation extension."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any
from collections.abc import Mapping

from .bhel_evaluation_common import (
    _DISABLED,
    _EVALUATION_SCOPES,
    _PROFILE_FILE,
    _PROFILE_NAMES,
    _model_data,
    RESULT_STATUSES,
)


def _profile_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / _PROFILE_FILE


def _resolve_profile_file(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    return cwd_path if cwd_path.exists() else Path(__file__).resolve().parents[1] / path


def _evaluation_scope(
    check: Mapping[str, Any],
    *,
    internal_ids: set[str] | None = None,
) -> str | None:
    """Resolve scope without allowing category inference to override metadata."""
    explicit = check.get("evaluation_scope")
    if explicit in (None, ""):
        extensions = check.get("extensions")
        evaluation = extensions.get("evaluation") if isinstance(extensions, Mapping) else None
        explicit = evaluation.get("evaluation_scope") if isinstance(evaluation, Mapping) else None
    if explicit not in (None, ""):
        normalized = str(explicit).strip().casefold()
        return normalized if normalized in _EVALUATION_SCOPES else None

    check_id = str(check.get("check_id") or "")
    kind = str(check.get("kind") or "").strip().casefold()
    category = str(check.get("category") or "").strip().casefold()
    if internal_ids and check_id in internal_ids:
        return "internal_control"
    if kind in {"internal_control", "internal", "internal_gate"} or category == "internal_control":
        return "internal_control"
    if kind in {"commercial", "commercial_check", "commercial_out_of_scope"}:
        return "commercial_out_of_scope"
    if category in {
        "commercial",
        "commercial_out_of_scope",
        "contractual",
        "price",
        "pricing",
    }:
        return "commercial_out_of_scope"
    return "technical_qualification"


def _reviewed_field_value(value: Any, field_key: str) -> Any:
    value = _model_data(value)
    if isinstance(value, Mapping):
        if str(value.get("key") or "") == field_key and "value" in value:
            return value.get("value")
        for child in value.values():
            found = _reviewed_field_value(child, field_key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _reviewed_field_value(child, field_key)
            if found is not None:
                return found
    return None


def _amount_in_inr(value: Any) -> float | None:
    text = json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value
    matches = re.findall(
        r"(?:inr|rs\.?|rupees?|\u20b9)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)?",
        text.casefold(),
    )
    amounts: list[float] = []
    for raw_number, raw_unit in matches:
        try:
            amount = float(raw_number.replace(",", ""))
        except ValueError:
            continue
        if raw_unit.startswith(("crore", "cr")):
            amount *= 10_000_000
        elif raw_unit.startswith(("lakh", "lac")):
            amount *= 100_000
        amounts.append(amount)
    return max(amounts) if amounts else None


def _applicability_rule_allows(check: Mapping[str, Any], understanding: Mapping[str, Any]) -> bool:
    rule = check.get("applicability_rule")
    if not isinstance(rule, Mapping):
        return True
    field_key = str(rule.get("field_key") or "")
    actual = _amount_in_inr(_reviewed_field_value(understanding, field_key)) if field_key else None
    if actual is None:
        return bool(rule.get("allow_when_unknown", False))
    try:
        expected = float(rule.get("value"))
    except (TypeError, ValueError):
        return False
    operator = str(rule.get("operator") or "").strip().casefold()
    return {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
        "eq": actual == expected,
    }.get(operator, False)


def _valid_profile(profile: Any) -> bool:
    if not isinstance(profile, dict):
        return False
    checks = profile.get("baseline_checks")
    if profile.get("schema_version") != "bhel-bid-evaluation.v1" or not isinstance(checks, list):
        return False
    check_ids = [str(item.get("check_id") or "") for item in checks if isinstance(item, dict)]
    source_rows = [item.get("source_row") for item in checks if isinstance(item, dict)]
    internal = {str(value) for value in profile.get("internal_control_ids") or []}
    expected_rows = set(range(1, 16)) | set(range(17, 36))
    return (
        len(check_ids) == 34
        and all(check_ids)
        and len(check_ids) == len(set(check_ids))
        and set(source_rows) == expected_rows
        and internal == {"qax_acceptance", "technical_acceptance", "pqr_verification"}
        and set(profile.get("result_statuses") or []) == set(RESULT_STATUSES)
        and all(
            not item.get("evaluation_scope")
            or item.get("evaluation_scope") in _EVALUATION_SCOPES
            for item in checks
            if isinstance(item, dict)
        )
    )


def load_bhel_profile() -> dict | None:
    """Load the profile only when explicitly enabled through environment."""
    selector = os.getenv("BID_EVALUATION_PROFILE", "").strip()
    configured_file = os.getenv("BID_EVALUATION_PROFILE_FILE", "").strip()
    if selector.casefold() in _DISABLED:
        return None

    if configured_file:
        path = _resolve_profile_file(configured_file)
    elif selector.casefold() in _PROFILE_NAMES:
        path = _profile_path()
    elif selector.lower().endswith(".json") or Path(selector).exists():
        path = _resolve_profile_file(selector)
    else:
        return None

    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not _valid_profile(profile):
        return None

    profile = deepcopy(profile)
    baseline = [item for item in profile.get("baseline_checks", []) if isinstance(item, dict)]
    internal_ids = {
        str(value)
        for value in profile.get("internal_control_ids", [])
        if isinstance(value, (str, int))
    }
    normalized_baseline: list[dict[str, Any]] = []
    for item in baseline:
        row = deepcopy(item)
        check_id = str(row.get("check_id") or "")
        row["check_id"] = check_id
        scope = _evaluation_scope(row, internal_ids=internal_ids)
        if scope:
            row["evaluation_scope"] = scope
            row["kind"] = "internal_control" if scope == "internal_control" else "bidder_check"
        else:
            row["kind"] = row.get("kind") or "bidder_check"
        normalized_baseline.append(row)
    profile["baseline_checks"] = normalized_baseline
    profile["baseline_rows"] = deepcopy(normalized_baseline)
    profile["all_bidder_checks"] = [
        deepcopy(row) for row in normalized_baseline if row.get("kind") != "internal_control"
    ]
    profile["bidder_checks"] = [
        deepcopy(row)
        for row in normalized_baseline
        if _evaluation_scope(row, internal_ids=internal_ids) == "technical_qualification"
    ]
    profile["technical_checks"] = deepcopy(profile["bidder_checks"])
    profile["internal_controls"] = [
        deepcopy(row)
        for row in normalized_baseline
        if _evaluation_scope(row, internal_ids=internal_ids) == "internal_control"
    ]
    profile["commercial_out_of_scope"] = [
        deepcopy(row)
        for row in normalized_baseline
        if _evaluation_scope(row, internal_ids=internal_ids) == "commercial_out_of_scope"
    ]
    profile["result_statuses"] = list(profile.get("result_statuses") or RESULT_STATUSES)
    return profile
