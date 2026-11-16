"""Deterministic tender identity and control projections."""

from __future__ import annotations

import re
from typing import Any

from .tender_field_projection_fields import field_entries


_GEM_BID_RE = re.compile(
    r"(?<![A-Za-z0-9])GEM\s*/\s*\d{4}\s*/\s*B\s*/\s*\d+(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_GENERIC_IDENTITY_VALUES = {
    "buyer",
    "buyer organization",
    "buyer organisation",
    "issuing authority",
    "organisation",
    "organization",
    "principal",
    "purchaser",
}
_EXPLICIT_CONTROL_PATTERNS = {
    "bid_structure_and_auction": (
        "Reverse auction",
        ("bid to ra enabled", "reverse auction enabled", "reverse auction"),
        ("reverse auction", "bid to ra enabled", "ra enabled"),
    ),
    "purchase_preference_and_allocation": (
        "Reserved for MSE",
        ("reserved for mse",),
        ("reserved for mse",),
    ),
}
_NO_EXEMPTION_PATTERNS = {
    "startup": (
        "startup exemption for years of experience and turnover",
        "startup exemption for years of experience",
        "startup exemption for turnover",
    ),
    "mse": (
        "mse exemption for years of experience and turnover",
        "mse exemption for years of experience",
        "mse exemption for turnover",
    ),
}


def _project_canonical_gem_identity(
    understanding: dict[str, Any], source_units: dict[str, dict[str, Any]]
) -> None:
    matches: dict[str, tuple[str, dict[str, Any]]] = {}
    for source_id, unit in source_units.items():
        text = str(unit.get("text") or "")
        for match in _GEM_BID_RE.finditer(text):
            number = re.sub(r"\s+", "", match.group(0)).upper()
            matches.setdefault(number, (source_id, unit))
    if len(matches) != 1:
        return
    number, (source_id, unit) = next(iter(matches.items()))
    for field in field_entries(understanding):
        if field.get("key") != "tender_identity":
            continue
        values = [
            str(value)
            for value in field.get("value") or []
            if not str(value).casefold().startswith("gem bid number:")
        ]
        field.update(
            status="found",
            value=[f"GeM Bid Number: {number}", *values],
            confidence=max(float(field.get("confidence") or 0), 0.99),
        )
        field["evidence"] = [
            {
                "source_id": source_id,
                "page_id": str(unit.get("page_id") or ""),
                "quote": str(unit.get("text") or "")[:300],
            },
            *(field.get("evidence") or []),
        ]
        field.setdefault("audit", {})["canonical_gem_projection"] = True
        return


def _generic_identity_value(value: Any) -> bool:
    text = str(value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return normalized in _GENERIC_IDENTITY_VALUES


def _identity_candidate(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" /|:-")
    if not text or _generic_identity_value(text) or len(text) < 4:
        return ""
    return text[:180]


def _issuer_from_source_units(
    source_units: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    candidates: list[tuple[int, str, str, dict[str, Any]]] = []
    for source_id, unit in source_units.items():
        try:
            page = int(unit.get("page") or unit.get("page_number") or 0)
        except (TypeError, ValueError):
            page = 0
        if page <= 0 or page > 2:
            continue
        raw = str(unit.get("text") or "")
        compact = re.sub(r"\s+", " ", raw).strip()
        if not compact:
            continue
        cells = [part.strip() for part in compact.split(" | ") if part.strip()]
        for index, cell in enumerate(cells[:-1]):
            if re.search(r"\b(?:organisation|organization)\s+name\b", cell, re.I):
                value = _identity_candidate(cells[index + 1])
                if value:
                    candidates.append((320, value, source_id, unit))
        match = re.search(
            r"\b(?:organisation|organization)\s+name\b\s*(?:[:/|=\-]\s*)?(.+?)"
            r"(?=\s+(?:office|department|total quantity|item category|bid number|dated)\b|$)",
            compact,
            re.I,
        )
        if match:
            value = _identity_candidate(match.group(1))
            if value:
                candidates.append((300, value, source_id, unit))
        if page == 1:
            header = re.search(
                r"\b([A-Z][A-Z0-9&.,'()/\- ]{4,100}\b(?:LIMITED|LTD\.?|CORPORATION|BANK|AUTHORITY))\b",
                raw[:900],
            )
            value = _identity_candidate(header.group(1) if header else "")
            if value:
                candidates.append((220, value, source_id, unit))
    if not candidates:
        return None
    groups: dict[str, list[tuple[int, str, str, dict[str, Any]]]] = {}
    for candidate in candidates:
        key = re.sub(r"\bltd\.?\b", "limited", candidate[1].casefold())
        key = re.sub(r"[^a-z0-9]+", " ", key).strip()
        groups.setdefault(key, []).append(candidate)
    best_group = max(
        groups.values(),
        key=lambda items: (
            max(item[0] for item in items),
            max(len(item[1]) for item in items),
        ),
    )
    _score, value, source_id, unit = max(
        best_group,
        key=lambda item: (item[0], len(item[1]), -int(item[3].get("page") or 0)),
    )
    return value, {
        "source_id": str(unit.get("source_id") or source_id),
        "page_id": str(unit.get("page_id") or ""),
        "quote": re.sub(r"\s+", " ", str(unit.get("text") or ""))[:500],
    }


def _explicit_control_matches(
    source_units: dict[str, dict[str, Any]],
    patterns: tuple[str, ...],
) -> list[tuple[str, dict[str, Any]]]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for source_id, unit in source_units.items():
        text = re.sub(r"[^a-z0-9]+", " ", str(unit.get("text") or "").casefold()).strip()
        for pattern in patterns:
            match = re.search(
                rf"\b{re.escape(pattern)}\b(?:\s+[a-z0-9]+){{0,4}}?\s+(yes|no)\b",
                text,
            )
            if match:
                matches.append(
                    (
                        match.group(1).title(),
                        {
                            "source_id": str(unit.get("source_id") or source_id),
                            "page_id": str(unit.get("page_id") or ""),
                            "quote": re.sub(r"\s+", " ", str(unit.get("text") or ""))[:500],
                        },
                    )
                )
                break
    return matches


def _project_deterministic_tender_controls(
    understanding: dict[str, Any], source_units: dict[str, dict[str, Any]]
) -> None:
    for field in field_entries(understanding):
        key = str(field.get("key") or "")
        if key == "tender_identity":
            issuer = _issuer_from_source_units(source_units)
            values = [
                str(value)
                for value in field.get("value") or []
                if not _generic_identity_value(value)
            ]
            if issuer:
                value, evidence = issuer
                values = [
                    item
                    for item in values
                    if not any(
                        label in item.casefold()
                        for label in ("issuing organization", "issuing organisation")
                    )
                ]
                values.insert(1 if values else 0, f"Issuing organization: {value}")
                field["status"] = "found"
                field["value"] = list(dict.fromkeys(values))
                field["evidence"] = [evidence, *(field.get("evidence") or [])]
                field.setdefault("audit", {})["deterministic_issuer_projection"] = True
            elif values:
                field["value"] = list(dict.fromkeys(values))
                field.setdefault("audit", {})["issuer_not_verified"] = True
            continue
        control = _EXPLICIT_CONTROL_PATTERNS.get(key)
        if not control:
            continue
        label, patterns, remove_labels = control
        matches = _explicit_control_matches(source_units, patterns)
        states = {state for state, _evidence in matches}
        if len(states) != 1:
            if len(states) > 1:
                field["status"] = "ambiguous"
                field["value"] = None
                field["notes"] = f"Conflicting explicit values found for {label}."
                field.setdefault("audit", {})["deterministic_control_conflict"] = True
            continue
        state = next(iter(states))
        values = [
            str(value)
            for value in field.get("value") or []
            if not any(remove_label in str(value).casefold() for remove_label in remove_labels)
        ]
        values.insert(0, f"{label}: {state}")
        field.update(
            status="found",
            value=list(dict.fromkeys(values)),
            evidence=[matches[0][1], *(field.get("evidence") or [])],
        )
        field.setdefault("audit", {})["deterministic_control_projection"] = True

    no_exemptions = {
        name
        for name, patterns in _NO_EXEMPTION_PATTERNS.items()
        if {
            state
            for state, _evidence in _explicit_control_matches(source_units, patterns)
        } == {"No"}
    }
    if not no_exemptions:
        return
    for field in field_entries(understanding):
        key = str(field.get("key") or "")
        if key not in {
            "financial_capacity",
            "minimum_industry_experience",
            "similar_project_qualification",
        }:
            continue

        def contradicted_exception(value: Any) -> bool:
            text = str(value or "").casefold()
            applies = any(term in text for term in ("applies", "applicable", "available", "granted"))
            if not applies or any(term in text for term in ("no exemption", "not applicable", "not granted")):
                return False
            return any(name in no_exemptions and name in text for name in ("startup", "mse"))

        exceptions = [
            item
            for item in field.get("exceptions") or []
            if not contradicted_exception(item)
        ]
        removed = len(field.get("exceptions") or []) - len(exceptions)
        if not removed:
            continue
        field["exceptions"] = exceptions
        conditions = [str(item) for item in field.get("conditions") or [] if str(item).strip()]
        explicit = (
            "Explicit tender control: "
            + " and ".join(sorted(name.upper() if name == "mse" else name.title() for name in no_exemptions))
            + " exemption for years of experience and turnover is No."
        )
        if explicit not in conditions:
            conditions.append(explicit)
        field["conditions"] = conditions
        field.setdefault("audit", {})["explicit_no_exemption_projection"] = {
            "controls": sorted(no_exemptions),
            "removed_contradictions": removed,
        }


def _project_source_table_ambiguities(
    understanding: dict[str, Any], source_units: dict[str, dict[str, Any]]
) -> None:
    """Surface contradictory source rows without inventing a preferred value."""
    for field in field_entries(understanding):
        audit = field.setdefault("audit", {})
        previous = audit.pop("source_table_ambiguities", [])
        value_text = str(field.get("value") or "").casefold()
        previous_applies = any(
            isinstance(anomaly, dict)
            and str(anomaly.get("label") or "").strip()
            and re.search(
                rf"\b{re.escape(str(anomaly.get('label')).casefold())}\b",
                value_text,
            )
            and all(
                token.strip()
                and re.search(
                    rf"(?<![\d.,]){re.escape(token.strip())}(?![\d.,])",
                    value_text,
                )
                for token in str(anomaly.get("values") or "").split(",")
            )
            for anomaly in previous
        )
        if audit.pop("source_table_ambiguity_projection", False) or (
            previous and field.get("status") == "ambiguous" and not previous_applies
        ):
            field["status"] = "found"
        if previous:
            notes = str(field.get("notes") or "")
            notes = re.sub(
                r"(?:^|\s)The source table lists .*?Confirm the intended row interpretation\.",
                " ",
                notes,
            )
            field["notes"] = " ".join(notes.split())

    tables: dict[str, list[tuple[str, str, str]]] = {}
    for unit in source_units.values():
        table_id = str(unit.get("table") or "").strip()
        if not table_id or str(unit.get("unit_type") or "") != "table_row":
            continue
        cells = [part.strip() for part in str(unit.get("text") or "").split("|")]
        if len(cells) < 3 or not re.search(r"\d", cells[-1]):
            continue
        label = cells[-2]
        core = re.sub(r"\([^)]*\)", " ", label)
        core = re.sub(r"[^A-Za-z0-9]+", " ", core).strip()
        value = " ".join(re.findall(r"\d+(?:[.,]\d+)?", cells[-1]))
        if core and value:
            tables.setdefault(table_id, []).append((core, value, str(unit.get("page_id") or "")))

    anomalies: list[dict[str, str]] = []
    for table_id, rows in tables.items():
        for index, (core, value, page_id) in enumerate(rows):
            if " " in core:
                continue
            for other_index, (other_core, other_value, other_page_id) in enumerate(rows):
                if index == other_index or value == other_value:
                    continue
                if re.search(rf"\b{re.escape(core)}\b", other_core, re.I):
                    anomalies.append(
                        {
                            "table_id": table_id,
                            "page_id": page_id or other_page_id,
                            "label": core,
                            "values": f"{other_value}, {value}",
                        }
                    )
                    break

    for anomaly in anomalies:
        for field in field_entries(understanding):
            searchable = str(field.get("value") or "").casefold()
            numeric_values = [value.strip() for value in anomaly["values"].split(",")]
            if (
                not re.search(rf"\b{re.escape(anomaly['label'].casefold())}\b", searchable)
                or not all(
                    re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d.,])", searchable)
                    for value in numeric_values
                )
            ):
                continue
            note = (
                f"The source table lists {anomaly['label']} in multiple rows with different "
                f"values ({anomaly['values']}). Confirm the intended row interpretation."
            )
            field["status"] = "ambiguous"
            field["notes"] = " ".join(
                part for part in (str(field.get("notes") or "").strip(), note) if part
            )
            audit = field.setdefault("audit", {})
            audit.setdefault("source_table_ambiguities", []).append(anomaly)
            audit["source_table_ambiguity_projection"] = True
