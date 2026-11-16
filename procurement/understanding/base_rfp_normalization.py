"""Deterministic Base RFP merge, normalization, and stable identifiers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

from .base_rfp_models import BaseRfpUnderstanding, NORMALIZATION_VERSION


_ID_PREFIXES = {
    "document_id": "doc",
    "fact_id": "fact",
    "party_id": "party",
    "lot_id": "lot",
    "deliverable_id": "deliverable",
    "event_id": "event",
    "clause_id": "clause",
    "requirement_id": "requirement",
    "criterion_id": "criterion",
    "policy_id": "policy",
    "required_document_id": "required_document",
    "annexure_id": "annexure",
    "definition_id": "definition",
    "gap_id": "gap",
}
_REFERENCE_TARGETS = {
    "document": "document_id",
    "document_ids": "document_id",
    "source_document_ids": "document_id",
    "supersedes_document_ids": "document_id",
    "uncovered_document_ids": "document_id",
    "fact_ids": "fact_id",
    "party_ids": "party_id",
    "lot_ids": "lot_id",
    "deliverable_ids": "deliverable_id",
    "dependency_event_ids": "event_id",
    "clause_ids": "clause_id",
    "requirement_ids": "requirement_id",
    "dependency_requirement_ids": "requirement_id",
    "criterion_ids": "criterion_id",
    "required_document_ids": "required_document_id",
    "annexure_ids": "annexure_id",
}

_IDENTITY_KEYS = {
    "document_id": ("checksum_sha256", "file_name", "title", "document_type"),
    "fact_id": ("key", "label", "value", "values", "unit", "condition"),
    "party_id": ("role", "name", "legal_name"),
    "lot_id": ("number", "title", "description"),
    "deliverable_id": ("name", "description", "quantity", "unit"),
    "event_id": ("event_type", "name", "start", "end", "duration"),
    "clause_id": ("number", "title", "text", "section_path", "clause_type"),
    "requirement_id": ("title", "text", "category", "stage", "actor", "action", "subject", "condition"),
    "criterion_id": ("name", "description", "stage", "weight", "maximum_score", "minimum_score"),
    "policy_id": ("name", "reference", "applicability"),
    "required_document_id": ("name", "category", "stage", "condition", "format"),
    "annexure_id": ("number", "title", "annexure_type", "description"),
    "definition_id": ("term", "definition"),
    "gap_id": ("gap_type", "description", "expected", "affected_paths", "affected_ids"),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, path: tuple[str, ...], value: Any) -> str:
    digest = sha256(_canonical({"path": path, "value": value}).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge(left: Any, right: Any) -> Any:
    if _blank(left):
        return deepcopy(right)
    if _blank(right) or left == right:
        return deepcopy(left)
    if isinstance(left, dict) and isinstance(right, dict):
        result = deepcopy(left)
        for key, value in right.items():
            result[key] = _merge(result[key], value) if key in result else deepcopy(value)
        return result
    if isinstance(left, list) and isinstance(right, list):
        return deepcopy(left) + deepcopy(right)
    # ponytail: scalar conflicts keep the first chunk; represent alternatives as facts when needed.
    return deepcopy(left)


def _scalar_conflicts(left: Any, right: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any, Any]]:
    if _blank(left) or _blank(right) or left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            conflict
            for key in left.keys() & right.keys()
            for conflict in _scalar_conflicts(left[key], right[key], path + (key,))
        ]
    if isinstance(left, list) or isinstance(right, list) or isinstance(left, dict) or isinstance(right, dict):
        return []
    if path and path[0] in {"schema_version", "record_id", "coverage", "audit"}:
        return []
    return [(path, left, right)]


def _normalize_set_lists(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _normalize_set_lists(item) for key, item in value.items()}
        for key in ("source_refs", *tuple(_REFERENCE_TARGETS)):
            items = result.get(key)
            if isinstance(items, list):
                result[key] = sorted({_canonical(item): item for item in items}.values(), key=_canonical)
        return result
    if isinstance(value, list):
        return [_normalize_set_lists(item) for item in value]
    return value


def _source_anchors(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: ref.get(key)
            for key in ("document", "page", "page_id", "block", "table", "cell")
            if ref.get(key) not in (None, "")
        }
        for ref in value.get("source_refs", [])
        if isinstance(ref, dict)
    ]


def _id_identity(id_field: str, value: Mapping[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    keys = _IDENTITY_KEYS[id_field]
    identity = {key: value.get(key) for key in keys if not _blank(value.get(key))}
    anchors = _source_anchors(value)
    if anchors:
        identity["source_anchors"] = anchors
    if not identity:
        identity["path"] = path
    return identity


def _assign_tree_ids(
    value: Any,
    path: tuple[str, ...] = (),
    *,
    include_relations: bool,
    allowed_fields: set[str] | None = None,
) -> tuple[Any, dict[str, dict[str, str]]]:
    maps: dict[str, dict[str, str]] = {field: {} for field in _ID_PREFIXES}

    def visit(item: Any, item_path: tuple[str, ...]) -> Any:
        if isinstance(item, list):
            rows = [visit(row, item_path + ("[]",)) for row in item]
            unique = {_canonical(row): row for row in rows}
            rows = list(unique.values())
            id_field = next((field for field in _ID_PREFIXES if rows and all(isinstance(row, dict) and field in row for row in rows)), None)
            return sorted(rows, key=lambda row: str(row[id_field])) if id_field else rows
        if not isinstance(item, dict):
            return item
        result = {key: visit(child, item_path + (key,)) for key, child in item.items()}
        id_field = None if item_path and item_path[0] == "audit" else next(
            (field for field in _ID_PREFIXES if field in result and (allowed_fields is None or field in allowed_fields)),
            None,
        )
        if id_field:
            old_id = str(result.get(id_field) or "")
            identity = _id_identity(id_field, result, item_path)
            new_id = _stable_id(_ID_PREFIXES[id_field], item_path, identity)
            result[id_field] = new_id
            if old_id:
                maps[id_field][old_id] = new_id
        return result

    return visit(value, path), maps


def _rewrite_references(value: Any, maps: Mapping[str, Mapping[str, str]]) -> Any:
    if isinstance(value, list):
        return [_rewrite_references(item, maps) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        target = _REFERENCE_TARGETS.get(key)
        replacements = maps.get(target, {}) if target else {}
        if target and isinstance(item, str):
            result[key] = replacements.get(item, item)
        elif target and isinstance(item, list):
            result[key] = sorted({replacements.get(str(entry), str(entry)) for entry in item if str(entry)})
        else:
            result[key] = _rewrite_references(item, maps)
    return result


def _combine_maps(*maps: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    combined = {field: {} for field in _ID_PREFIXES}
    for mapping in maps:
        for field, replacements in mapping.items():
            combined[field].update(replacements)
    return combined


def _count_source_refs(value: Any) -> int:
    if isinstance(value, list):
        return sum(_count_source_refs(item) for item in value)
    if not isinstance(value, dict):
        return 0
    return len(value.get("source_refs", [])) + sum(
        _count_source_refs(item) for key, item in value.items() if key != "source_refs"
    )


def normalize_base_rfp_partial(partial: Mapping[str, Any] | BaseRfpUnderstanding) -> dict[str, Any]:
    """Validate one partial and return only fields explicitly supplied by it."""
    model = partial if isinstance(partial, BaseRfpUnderstanding) else BaseRfpUnderstanding.model_validate(partial)
    return model.model_dump(mode="json", exclude_unset=True, exclude_defaults=True)


def assign_stable_ids(data: Mapping[str, Any] | BaseRfpUnderstanding) -> dict[str, Any]:
    """Return a validated copy with deterministic content IDs and exact deduplication."""
    model = data if isinstance(data, BaseRfpUnderstanding) else BaseRfpUnderstanding.model_validate(data)
    result = _normalize_set_lists(model.model_dump(mode="json"))

    source_documents, document_maps = _assign_tree_ids(
        result["source_documents"],
        ("source_documents",),
        include_relations=False,
        allowed_fields={"document_id"},
    )
    result["source_documents"] = source_documents
    result = _rewrite_references(result, document_maps)

    other_fields = set(_ID_PREFIXES) - {"document_id"}
    result, provisional_maps = _assign_tree_ids(
        result, include_relations=False, allowed_fields=other_fields
    )
    result = _rewrite_references(result, provisional_maps)
    result, final_maps = _assign_tree_ids(
        result, include_relations=True, allowed_fields=other_fields
    )
    result = _rewrite_references(result, _combine_maps(document_maps, final_maps))
    result = _normalize_set_lists(result)

    coverage = result["coverage"]
    coverage["documents_total"] = len(result["source_documents"])
    coverage["clauses_total"] = len(result["clauses"])
    coverage["requirements_total"] = len(result["requirements"])
    coverage["source_refs_total"] = _count_source_refs(result)

    payload = {key: value for key, value in result.items() if key not in {"record_id", "audit"}}
    digest = sha256(_canonical(payload).encode()).hexdigest()
    result["record_id"] = f"rfp_{digest[:16]}"
    result["audit"]["normalization_version"] = NORMALIZATION_VERSION
    result["audit"]["content_sha256"] = digest
    return BaseRfpUnderstanding.model_validate(result).model_dump(mode="json")


def merge_base_rfp_partials(
    partials: Iterable[Mapping[str, Any] | BaseRfpUnderstanding],
) -> dict[str, Any]:
    """Merge chunk partials without semantic clause or requirement collapse."""
    normalized = [normalize_base_rfp_partial(partial) for partial in partials]
    merged: dict[str, Any] = {}
    input_record_ids: list[str] = []
    conflicts: list[tuple[tuple[str, ...], Any, Any]] = []
    for partial in normalized:
        if partial.get("record_id"):
            input_record_ids.append(str(partial["record_id"]))
        conflicts.extend(_scalar_conflicts(merged, partial))
        merged = _merge(merged, partial)
    if conflicts:
        merged.setdefault("gaps", []).extend(
            {
                "gap_type": "conflicting_extraction",
                "severity": "high",
                "description": f"Conflicting values extracted for {'.'.join(path)}.",
                "expected": _canonical(right)[:500],
                "impact": f"Retained first value {_canonical(left)[:500]}; conflicting value requires reconciliation.",
                "affected_paths": [".".join(path)],
            }
            for path, left, right in conflicts
        )
    merged.setdefault("audit", {})["chunk_count"] = len(normalized)
    merged["audit"]["input_record_ids"] = sorted(set(input_record_ids))
    return assign_stable_ids(BaseRfpUnderstanding.model_validate(merged))


def create_base_rfp_understanding(
    partials: Iterable[Mapping[str, Any] | BaseRfpUnderstanding] | Mapping[str, Any] | BaseRfpUnderstanding,
) -> BaseRfpUnderstanding:
    """Normalize, merge, assign IDs, and return the validated Base RFP record."""
    if isinstance(partials, (Mapping, BaseRfpUnderstanding)):
        partials = [partials]
    return BaseRfpUnderstanding.model_validate(merge_base_rfp_partials(partials))


__all__ = [
    "assign_stable_ids",
    "create_base_rfp_understanding",
    "merge_base_rfp_partials",
    "normalize_base_rfp_partial",
]

