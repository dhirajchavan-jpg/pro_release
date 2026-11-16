"""Document-role parsing and grounded amendment event candidates."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .field_core import (
    _DELETE_CUES,
    _amendment_source_binds_field,
    _clarification_target_matches_field,
    _clean_list,
    _compact_text,
    _direct_scalar_update_value,
    _field_presence_match,
    _field_subject_compatible,
    _formal_revision_binds_field,
    _formal_revision_cells,
    _formal_revision_target_text,
    _has_compatible_numeric_claim,
    _issuer_response_is_header,
    _issuer_response_matches_field_anchor,
    _link_amendment_target,
    _merge_table_row_continuations,
    _phrase_text,
    _pointer_only_amendment_response,
    _scope_contains,
    _short_binding_field_response,
    _tokens,
    _unit_document,
    _unit_page,
    _unit_page_id,
    _unit_text,
)
from .field_retrieval import LocalFieldRetriever
from .schema import RfpFieldSpec

def _replacement_omission_events(
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    retriever: LocalFieldRetriever,
) -> list[dict[str, Any]]:
    evidence_ids = {str(item.get("source_id") or "") for item in evidence}
    events: list[dict[str, Any]] = []

    def material_historical_value(unit: dict[str, Any]) -> bool:
        headers = _phrase_text(" ".join(str(item) for item in unit.get("table_headers") or []))
        if "response" in headers and any(
            cue in headers for cue in ("query", "request", "clarification")
        ):
            return False
        if field.value_type in {"boolean", "integer", "number", "money", "date", "datetime"}:
            return bool(_direct_scalar_update_value(field, unit))
        return _field_presence_match(unit, field)

    for scope, replacement_document in retriever.latest_replacement_document.items():
        replacement_rank = retriever.document_authority.get(replacement_document, 0)
        marker_ids = [
            source_id
            for source_id in retriever.replacement_marker_ids.get(
                (scope, replacement_document),
                [],
            )
            if source_id in evidence_ids
        ]
        if not marker_ids:
            continue
        historical = [
            (source_id, unit)
            for source_id, unit in retriever.units.items()
            if _scope_contains(scope, str(unit.get("section_scope") or ""))
            and retriever.document_authority.get(_unit_document(unit), 0) < replacement_rank
            and material_historical_value(unit)
        ]
        replacement_snapshot = [
            unit
            for unit in retriever.units.values()
            if _unit_document(unit) == replacement_document
            and _scope_contains(scope, str(unit.get("section_scope") or ""))
            and not unit.get("replacement_scope")
        ]
        if not historical or any(
            _field_presence_match(unit, field) for unit in replacement_snapshot
        ):
            continue
        previous_id, previous = max(
            historical,
            key=lambda item: (
                retriever.document_authority.get(_unit_document(item[1]), 0),
                retriever.position.get(item[0], 0),
            ),
        )
        events.append(
            {
                "operation": "delete",
                "target": field.key,
                "previous_text": _unit_text(previous),
                "effective_text": "",
                "required_documents": [],
                "source_ids": marker_ids[:2],
                "confidence": 1.0,
                "target_source_ids": [previous_id],
                "scope_replacement": True,
            }
        )
    return events
_AMENDMENT_OPERATIONS = {"add", "replace", "delete", "retain", "clarify"}
_AMENDMENT_SOURCE_KIND_RANK = {
    "retention_response": 0,
    "binding_clarification": 1,
    "explicit_update": 2,
    "formal_revision": 3,
}

def amendment_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["no_change", "resolved", "unresolved"],
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": sorted(_AMENDMENT_OPERATIONS),
                        },
                        "target": {"type": "string", "maxLength": 300},
                        "previous_text": {"type": "string", "maxLength": 700},
                        "effective_text": {"type": "string", "maxLength": 1000},
                        "required_documents": {
                            "type": "array",
                            "maxItems": 16,
                            "items": {"type": "string"},
                        },
                        "source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "operation",
                        "target",
                        "previous_text",
                        "effective_text",
                        "required_documents",
                        "source_ids",
                        "confidence",
                    ],
                },
            },
            "image_page_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "search_query": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["status", "events", "image_page_ids", "search_query", "notes"],
    }

def _amendment_source_kind(items: list[dict[str, Any]]) -> str:
    headers = _phrase_text(
        " ".join(str(header) for item in items for header in item.get("table_headers") or [])
    )
    text = _phrase_text(" ".join(str(item.get("text") or "") for item in items))
    if (
        ("existing" in headers or "current clause" in headers)
        and ("revised" in headers or "modified clause" in headers or "amended clause" in headers)
    ):
        return "formal_revision"
    if "response" in headers and any(word in headers for word in ("query", "request", "clarification")):
        if any(phrase in text for phrase in ("as per rfp", "as per rfe", "no change")):
            return "retention_response"
        return "binding_clarification"
    return "explicit_update"

def _normalize_amendment_response(
    raw: dict[str, Any],
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    retriever: LocalFieldRetriever,
) -> tuple[list[dict[str, Any]], str, list[str], list[str]]:
    by_id = {str(item.get("source_id") or ""): item for item in evidence}
    for parent in evidence:
        for continuation_id in parent.get("table_continuation_source_ids") or []:
            continuation_id = str(continuation_id)
            unit = retriever.units.get(continuation_id)
            if not unit or unit.get("source_role") != "post_base_update":
                continue
            revision = _formal_revision_cells(unit)
            by_id.setdefault(
                continuation_id,
                {
                    **unit,
                    "source_id": continuation_id,
                    "document_id": _unit_document(unit),
                    "page": _unit_page(unit),
                    "page_id": _unit_page_id(unit),
                    "source_role": "post_base_update",
                    "revision_sequence": unit.get("revision_sequence"),
                    "authority_rank": unit.get("authority_rank"),
                    "text": _unit_text(unit),
                    "formal_revision": bool(revision),
                    "existing_text": revision[0] if revision else "",
                    "revised_text": revision[1] if revision else "",
                },
            )
    allowed = {
        source_id
        for source_id, item in by_id.items()
        if item.get("source_role") == "post_base_update"
    }
    warnings: list[str] = []

    def grounded_continuation_ids(source_id: str) -> list[str]:
        source = by_id.get(source_id, {})
        revision_source_ids = source.get("revision_source_ids") or []
        if revision_source_ids:
            return [str(item) for item in revision_source_ids]
        if (
            field.value_type
            in {"boolean", "integer", "number", "money", "date", "datetime"}
            and source.get("formal_revision")
            and source.get("revision_layout")
            in {
                "aligned_columns",
                "implicit_parallel_columns",
                "parallel_nested_columns",
            }
        ):
            return []
        return [
            str(item) for item in source.get("table_continuation_source_ids") or []
        ]

    raw_status = str(raw.get("status") or "unresolved")
    if raw.get("notes") and raw_status not in {"resolved", "no_change"}:
        warnings.append(_compact_text(raw.get("notes"))[:500])
    events: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("events") or [], start=1):
        if not isinstance(item, dict):
            warnings.append(f"Ignored malformed amendment event {index}.")
            continue
        operation = str(item.get("operation") or "")
        source_ids = [
            str(source_id)
            for source_id in item.get("source_ids") or []
            if str(source_id) in allowed
        ]
        source_ids = list(dict.fromkeys(source_ids))
        source_ids = list(
            dict.fromkeys(
                [
                    expanded_source_id
                    for source_id in source_ids
                    for expanded_source_id in [
                        source_id,
                        *[
                            str(continuation_id)
                            for continuation_id in grounded_continuation_ids(source_id)
                            if str(continuation_id) in allowed
                        ],
                    ]
                ]
            )
        )
        if operation not in _AMENDMENT_OPERATIONS or not source_ids:
            warnings.append(f"Ignored unsupported or ungrounded amendment event {index}.")
            continue
        cited = [by_id[source_id] for source_id in source_ids]
        if cited and all(source.get("embedded_revision_fragment") for source in cited):
            warnings.append(
                f"Ignored amendment event {index} sourced only from an embedded revision fragment."
            )
            continue
        configured_target = _phrase_text(str(item.get("target") or "")) in {
            _phrase_text(field.key.replace("_", " ")),
            _phrase_text(field.label),
        }
        scope_omission = bool(
            operation == "delete"
            and item.get("scope_replacement")
            and any(source.get("replacement_scope") for source in cited)
        )
        if not scope_omission:
            binding_ids = {
                str(source.get("source_id") or "")
                for source in cited
                if _amendment_source_binds_field(field, source, retriever)
                or (
                    configured_target
                    and bool(source.get("amendment_candidate"))
                    and _field_subject_compatible(field, _unit_text(source))
                )
                or (
                    field.value_type in {"string_array", "object", "object_array"}
                    and source.get("amendment_candidate")
                )
            }
            if not binding_ids:
                warnings.append(
                    f"Ignored amendment event {index} whose cited source does not own this field."
                )
                continue
            structural_ids = set(binding_ids)
            for source_id in list(binding_ids):
                structural_ids.update(
                    str(continuation_id)
                    for continuation_id in by_id[source_id].get(
                        "table_continuation_source_ids"
                    )
                    or []
                )
                parent_id = retriever.continuation_parent.get(source_id)
                if parent_id:
                    structural_ids.add(parent_id)
                    structural_ids.update(retriever.table_continuations.get(parent_id, []))
                structural_ids.update(retriever.table_continuations.get(source_id, []))
            source_ids = [
                source_id for source_id in source_ids if source_id in structural_ids
            ]
            cited = [by_id[source_id] for source_id in source_ids]
        target = _compact_text(item.get("target"))[:500]
        formal_fragments: list[tuple[str, str]] = []
        seen_formal_fragments: set[tuple[str, str]] = set()
        for source in sorted(
            cited,
            key=lambda value: retriever.position.get(
                str(value.get("source_id") or ""), 0
            ),
        ):
            revision = _formal_revision_cells(source)
            if not revision:
                continue
            normalized_revision = (
                _phrase_text(revision[0]),
                _phrase_text(revision[1]),
            )
            if normalized_revision in seen_formal_fragments:
                continue
            seen_formal_fragments.add(normalized_revision)
            formal_fragments.append(revision)
        if operation == "replace" and len(formal_fragments) >= 2:
            previous_text = _compact_text(
                " ".join(fragment[0] for fragment in formal_fragments)
            )[:4000]
            effective_text = _compact_text(
                " ".join(fragment[1] for fragment in formal_fragments)
            )[:5000]
        else:
            previous_text = _compact_text(item.get("previous_text"))[:1200]
            continuation_revision = (
                operation in {"add", "replace"}
                and len(cited) >= 2
                and any(source.get("formal_revision") for source in cited)
            )
            effective_text = _compact_text(item.get("effective_text"))[
                : 5000 if continuation_revision else 1800
            ]
        if operation == "replace" and any(
            cue in effective_text.casefold() for cue in _DELETE_CUES
        ):
            operation = "delete"
        if operation == "delete":
            effective_text = ""
        if operation in {"add", "replace", "clarify"} and not effective_text:
            warnings.append(f"Ignored amendment event {index} with no effective text.")
            continue
        target_source_ids, target_status = _link_amendment_target(
            field, target, previous_text, evidence
        )
        if scope_omission:
            replacement_scopes = {
                str(source.get("section_scope") or "")
                for source in cited
                if source.get("replacement_scope") and source.get("section_scope")
            }
            provided_targets = [
                str(source_id)
                for source_id in item.get("target_source_ids") or []
                if str(source_id) in retriever.units
                and retriever.document_authority.get(
                    _unit_document(retriever.units[str(source_id)]), 0
                )
                < max(int(source.get("authority_rank") or 0) for source in cited)
                and retriever.units[str(source_id)].get("effective_state")
                == "superseded"
                and any(
                    _scope_contains(
                        replacement_scope,
                        str(
                            retriever.units[str(source_id)].get("section_scope") or ""
                        ),
                    )
                    for replacement_scope in replacement_scopes
                )
            ]
            if not provided_targets:
                warnings.append(
                    f"Ignored scope-replacement delete {index} with no prior base clause in the same section scope."
                )
                continue
            target_source_ids = list(dict.fromkeys(provided_targets))
            target_status = "linked"
        if operation == "add" and target_status == "unresolved":
            target_status = "new_target"
        source_kind = _amendment_source_kind(cited)
        if target_status == "unresolved":
            if any(
                _amendment_source_binds_field(field, source, retriever)
                for source in cited
            ):
                target_status = "configured_field"
        authority_rank = max(int(source.get("authority_rank") or 0) for source in cited)
        source_order = max(
            retriever.position.get(str(source.get("source_id") or ""), 0)
            for source in cited
        )
        section_scopes = sorted(
            {
                str(source.get("section_scope") or "")
                for source in cited
                if source.get("section_scope")
            }
        )
        validated_scope_replacement = bool(
            scope_omission
            or (
                operation == "replace"
                and item.get("scope_replacement")
                and any(
                    _scope_contains(replacement_scope, source_scope)
                    and replacement_document == _unit_document(source)
                    for source in cited
                    for source_scope in [str(source.get("section_scope") or "")]
                    for replacement_scope, replacement_document in retriever.latest_replacement_document.items()
                )
            )
        )
        if target_status == "unresolved" and validated_scope_replacement:
            target_status = "configured_field"
        normalized_target = _phrase_text(target)[:160]
        scalar_field = field.value_type in {
            "boolean",
            "integer",
            "number",
            "money",
            "date",
            "datetime",
        }
        if scalar_field:
            target_key = field.key
        elif target_source_ids:
            target_key = "base:" + ",".join(sorted(target_source_ids))
        elif target_status == "configured_field":
            configured_targets = {
                _phrase_text(field.key.replace("_", " ")),
                _phrase_text(field.label),
            }
            target_key = (
                field.key
                if normalized_target in configured_targets
                else f"{field.key}:{normalized_target or 'field'}"
            )
        else:
            target_key = normalized_target or field.key
        event_seed = {
            "field_key": field.key,
            "operation": operation,
            "target_key": target_key,
            "target": target,
            "effective_text": effective_text,
            "source_ids": source_ids,
        }
        event_id = "amend-" + sha256(
            json.dumps(event_seed, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        events.append(
            {
                "event_id": event_id,
                "field_key": field.key,
                "operation": operation,
                "target_key": target_key,
                "target": target,
                "previous_text": previous_text,
                "effective_text": effective_text,
                "required_documents": _clean_list(item.get("required_documents"), limit=16),
                "source_ids": source_ids,
                "page_ids": list(
                    dict.fromkeys(
                        str(source.get("page_id") or "")
                        for source in cited
                        if source.get("page_id")
                    )
                ),
                "target_source_ids": target_source_ids,
                "target_status": target_status,
                "source_kind": source_kind,
                "authority_rank": authority_rank,
                "source_order": source_order,
                "scope_replacement": validated_scope_replacement,
                "section_scopes": section_scopes,
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
            }
        )
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for event in sorted(
        events,
        key=lambda value: (
            int(value.get("authority_rank") or 0),
            int(value.get("source_order") or 0),
        ),
    ):
        identity = (
            event["operation"],
            event["target_key"],
            _phrase_text(event["effective_text"]),
            event["authority_rank"],
        )
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(event)
    valid_pages = {str(item.get("page_id") or "") for item in evidence if item.get("page_id")}
    requested_images = [
        str(page_id)
        for page_id in raw.get("image_page_ids") or []
        if str(page_id) in valid_pages
    ][:2]
    status = raw_status
    if status not in {"no_change", "resolved", "unresolved"}:
        warnings.append(f"Invalid amendment response status: {status}.")
        status = "unresolved"
    if status == "unresolved" and not deduplicated:
        note = _phrase_text(str(raw.get("notes") or ""))
        absence_cues = (
            "no binding amendment",
            "do not contain any information",
            "does not contain any information",
            "no specific amendment",
            "no amendment events",
        )
        unresolved_cues = (
            "refer corrigendum",
            "refer correction",
            "conflict",
            "contradict",
            "incomplete",
            "not visible",
            "missing revised",
        )
        if not raw.get("search_query") and any(cue in note for cue in absence_cues) and not any(
            cue in note for cue in unresolved_cues
        ):
            status = "no_change"
    return deduplicated, status, requested_images, warnings

def _deterministic_amendment_events(
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    retriever: LocalFieldRetriever,
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_events = _replacement_omission_events(field, evidence, retriever)
    standalone_event_sources: set[frozenset[str]] = set()
    for item in evidence:
        if item.get("source_role") != "post_base_update":
            continue
        source_id = str(item.get("source_id") or "")
        text = str(item.get("text") or "")
        revision = (
            _formal_revision_cells(item)
            if item.get("formal_revision")
            else None
        )
        scalar_value = _direct_scalar_update_value(field, item)
        scope = str(item.get("section_scope") or "")
        replacement_document = next(
            (
                document
                for replacement_scope, document in retriever.latest_replacement_document.items()
                if _scope_contains(replacement_scope, scope)
                and document == _unit_document(item)
            ),
            "",
        )
        section_text = _phrase_text(
            " ".join(str(part) for part in item.get("section_path") or [])
        )
        likely_section = not field.retrieval_guidance.likely_sections or any(
            _phrase_text(candidate) in section_text
            for candidate in field.retrieval_guidance.likely_sections
        )
        field_prompt = _phrase_text(field.prompt)
        technical_section = "technical evaluation" in section_text
        technical_requested = (
            "technical" in field_prompt
            and not (
                "technical scoring bands" in field_prompt
                and "are not" in field_prompt
            )
        )
        field_signature = set(_tokens(f"{field.key} {field.label}"))
        facet_specific = field.value_type in {"string_array", "object_array"} and any(
            set(_tokens(str(facet))) & field_signature
            for facet in item.get("facet_matches") or []
        )
        standalone_replacement = bool(
            replacement_document
            and item.get("unit_type") == "table_row"
            and item.get("effective_state") != "superseded"
            and (likely_section or (technical_section and technical_requested))
            and _field_subject_compatible(field, text)
            and (
                item.get("direct_field_match")
                or item.get("anchor_match")
                or facet_specific
            )
            and not any(cue in text.casefold() for cue in _DELETE_CUES)
        )
        if revision:
            if not _formal_revision_binds_field(field, item, retriever):
                continue
            previous_absent = _phrase_text(revision[0]) in {
                "",
                "nil",
                "none",
                "not applicable",
                "new clause",
                "additional clause",
            }
            operation = (
                "delete"
                if any(cue in revision[1].casefold() for cue in _DELETE_CUES)
                else "add"
                if previous_absent
                else "replace"
            )
            continuation_text = (
                ""
                if field.value_type
                in {"boolean", "integer", "number", "money", "date", "datetime"}
                else _compact_text(
                    " ".join(
                        _unit_text(continuation)
                        for continuation_id in item.get("table_continuation_source_ids") or []
                        if (continuation := retriever.units.get(str(continuation_id)))
                        and _unit_document(continuation) == _unit_document(item)
                        and _unit_page(item)
                        < _unit_page(continuation)
                        <= _unit_page(item) + 1
                        and _formal_revision_cells(continuation) is None
                    )
                )
            )
            raw_events.append(
                {
                    "operation": operation,
                    "target": _formal_revision_target_text(item) or field.key,
                    "previous_text": revision[0],
                    "effective_text": _compact_text(
                        f"{revision[1]} {continuation_text}"
                    ),
                    "required_documents": [],
                    "source_ids": list(
                        dict.fromkeys(
                            [source_id, *list(item.get("revision_source_ids") or [])]
                        )
                    ),
                    "confidence": 0.99,
                }
            )
        elif scalar_value:
            base_candidates = [
                base
                for base in evidence
                if base.get("source_role") == "base_rfp"
                and _direct_scalar_update_value(field, base)
            ]
            previous_text = _compact_text(
                max(
                    base_candidates,
                    key=lambda base: int(base.get("authority_rank") or 0),
                    default={},
                ).get("text")
            )
            raw_events.append(
                {
                    "operation": "replace" if previous_text else "add",
                    "target": field.key,
                    "previous_text": previous_text,
                    "effective_text": scalar_value,
                    "required_documents": [],
                    "source_ids": [source_id],
                    "confidence": 1.0,
                }
            )
        elif standalone_replacement:
            parent_id = retriever.continuation_parent.get(source_id)
            related_source_ids = list(
                dict.fromkeys(
                    [
                        *([parent_id] if parent_id else []),
                        source_id,
                        *retriever.table_continuations.get(parent_id or source_id, []),
                    ]
                )
            )
            source_identity = frozenset(related_source_ids)
            if source_identity not in standalone_event_sources:
                standalone_event_sources.add(source_identity)
                canonical = retriever.units.get(parent_id or source_id, item)
                canonical_scope = str(canonical.get("section_scope") or scope)
                continuation_units = [
                    retriever.units[related_source_id]
                    for related_source_id in related_source_ids
                    if related_source_id != (parent_id or source_id)
                    and related_source_id in retriever.units
                ]
                raw_events.append(
                    {
                        "operation": "replace",
                        "target": (
                            f"{canonical_scope}:"
                            f"{canonical.get('clause_number') or field.key}"
                        ),
                        "previous_text": "",
                        "effective_text": _merge_table_row_continuations(
                            canonical,
                            continuation_units,
                        ),
                        "required_documents": [],
                        "source_ids": related_source_ids,
                        "scope_replacement": True,
                        "confidence": 0.99,
                    }
                )
        elif not _amendment_source_binds_field(field, item, retriever):
            continue
        elif any(cue in text.casefold() for cue in _DELETE_CUES):
            raw_events.append(
                {
                    "operation": "delete",
                    "target": field.key,
                    "previous_text": text,
                    "effective_text": "",
                    "required_documents": [],
                    "source_ids": [source_id],
                    "confidence": 0.99,
                }
            )
        else:
            issuer_response = _compact_text(
                item.get("issuer_response")
                or item.get("issuer_response_or_revised_text")
            )
            if _issuer_response_is_header(issuer_response):
                continue
            response_matches = _issuer_response_matches_field_anchor(
                field, issuer_response
            )
            short_binding_response = _short_binding_field_response(
                field, item, issuer_response
            )
            target_matches = _clarification_target_matches_field(field, item)
            response_tokens = _tokens(issuer_response)
            pointer_only = _pointer_only_amendment_response(issuer_response)
            if (
                issuer_response
                and not pointer_only
                and (response_matches or short_binding_response or target_matches)
                and (len(response_tokens) >= 8 or short_binding_response)
            ):
                raw_events.append(
                    {
                        "operation": "clarify",
                        "target": field.key,
                        "previous_text": _compact_text(
                            item.get("bidder_request")
                            or item.get("bidder_request_or_prior_text")
                        ),
                        "effective_text": issuer_response,
                        "required_documents": [],
                        "source_ids": [source_id],
                        "confidence": 0.99,
                    }
                )
    if not raw_events:
        return [], []
    events, _status, _images, warnings = _normalize_amendment_response(
        {
            "status": "resolved",
            "events": raw_events,
            "image_page_ids": [],
            "notes": "",
        },
        field,
        evidence,
        retriever,
    )
    return events, warnings

def _residual_amendment_evidence(
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    deterministic_events: list[dict[str, Any]],
    retriever: LocalFieldRetriever,
) -> list[dict[str, Any]]:
    """Keep only amendment rows not already compiled, plus compact base context."""

    covered_source_ids = {
        str(source_id)
        for event in deterministic_events
        for source_id in event.get("source_ids") or []
    }
    scalar_field = field.value_type in {
        "boolean",
        "integer",
        "number",
        "money",
        "date",
        "datetime",
    }
    latest_deterministic_event = max(
        deterministic_events,
        key=lambda event: (
            int(event.get("authority_rank") or 0),
            int(event.get("source_order") or 0),
        ),
        default={},
    )
    latest_deterministic_rank = (
        int(latest_deterministic_event.get("authority_rank") or 0),
        int(latest_deterministic_event.get("source_order") or 0),
    )

    def can_compete_with_latest(item: dict[str, Any]) -> bool:
        if not deterministic_events:
            return True
        source_id = str(item.get("source_id") or "")
        item_rank = (
            int(item.get("authority_rank") or 0),
            int(item.get("source_order") or retriever.position.get(source_id, 0)),
        )
        if (
            latest_deterministic_event.get("operation") == "delete"
            and item_rank < latest_deterministic_rank
        ):
            return False
        if scalar_field and item_rank < latest_deterministic_rank:
            return False
        if field.value_type in {"integer", "number"}:
            return _has_compatible_numeric_claim(field, _unit_text(item))
        return True

    residual = [
        item
        for item in evidence
        if item.get("source_role") == "post_base_update"
        and not item.get("embedded_revision_fragment")
        and item.get("effective_state") != "superseded"
        and item.get("amendment_candidate")
        and str(item.get("source_id") or "") not in covered_source_ids
        and (
            field.value_type in {"string_array", "object", "object_array"}
            or _amendment_source_binds_field(field, item, retriever)
        )
        and can_compete_with_latest(item)
    ]
    base = [item for item in evidence if item.get("source_role") == "base_rfp"][:4]
    return [*residual, *base]
