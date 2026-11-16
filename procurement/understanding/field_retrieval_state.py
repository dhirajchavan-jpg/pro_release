"""Effective-state compilation for retrieved field evidence."""

from __future__ import annotations

from typing import Any

from .field_core import (
    _DELETE_CUES,
    _EXPLICIT_UPDATE_CUES,
    _direct_field_match,
    _field_presence_match,
    _formal_revision_cells,
    _phrase_text,
)

from .schema import (
    RfpFieldSpec,
)


class FieldRetrievalState:
    """Methods for this retrieval responsibility."""

    def compile_effective_state(
        self,
        evidence: list[dict[str, Any]],
        field: RfpFieldSpec | None = None,
        package_field_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if package_field_state and package_field_state.get("status") != "not_compiled":
            all_ids = {
                str(item.get("source_id") or "")
                for item in evidence
                if item.get("source_id")
            }
            events = [
                item
                for item in package_field_state.get("events") or []
                if isinstance(item, dict)
            ]
            effective_event_ids = set(package_field_state.get("effective_event_ids") or [])
            effective_events = [
                item for item in events if item.get("event_id") in effective_event_ids
            ]
            current_event_sources = {
                str(source_id)
                for item in effective_events
                for source_id in item.get("source_ids") or []
            }
            historical_ids = {
                str(source_id)
                for source_id in package_field_state.get("historical_source_ids") or []
            }
            mutation_target_ids = {
                str(source_id)
                for item in effective_events
                if item.get("operation") in {"replace", "delete"}
                for source_id in item.get("target_source_ids") or []
            }
            retained_target_ids = {
                str(source_id)
                for item in effective_events
                if item.get("operation") in {"retain", "add", "clarify"}
                for source_id in item.get("target_source_ids") or []
            }
            deterministic_superseded = {
                str(item.get("source_id") or "")
                for item in evidence
                if item.get("effective_state") == "superseded"
            }
            historical_ids.update(mutation_target_ids)
            historical_ids.update(deterministic_superseded)
            current_event_sources -= deterministic_superseded
            retained_target_ids -= historical_ids
            current_ids = (
                (all_ids - historical_ids)
                | current_event_sources
                | retained_target_ids
            ) - deterministic_superseded

            direct_ids = {
                str(item.get("source_id") or "")
                for item in evidence
                if field is None
                or item.get("direct_field_match")
                or (
                    item.get("source_role") == "base_rfp"
                    and _direct_field_match(item, field)
                )
                or _field_presence_match(item, field)
            }
            has_mutating_event = any(
                item.get("operation") in {"replace", "delete"}
                for item in effective_events
            )
            base_direct_ids = {
                str(item.get("source_id") or "")
                for item in evidence
                if item.get("source_role") == "base_rfp"
                and item.get("source_id") in direct_ids
                and item.get("source_id") in current_ids
            }
            if current_event_sources and has_mutating_event:
                controlling_ids = (current_event_sources | retained_target_ids) & current_ids
            elif current_event_sources:
                controlling_ids = (
                    base_direct_ids | current_event_sources | retained_target_ids
                ) & current_ids
            else:
                controlling_ids = base_direct_ids
            documents = {
                str(item.get("document_id") or ""): {
                    "document_id": str(item.get("document_id") or ""),
                    "source_file": str(item.get("source_file") or ""),
                    "source_role": str(item.get("source_role") or "unknown"),
                    "revision_sequence": item.get("revision_sequence"),
                    "document_order": item.get("document_order"),
                    "authority_rank": item.get("authority_rank"),
                }
                for item in evidence
                if item.get("document_id")
            }

            def render_event(item: dict[str, Any]) -> dict[str, Any]:
                complex_field = field is not None and field.value_type in {
                    "string",
                    "string_array",
                    "object",
                    "object_array",
                }
                return {
                    "event_id": item.get("event_id"),
                    "operation": item.get("operation"),
                    "target": item.get("target"),
                    "source_ids": item.get("source_ids") or [],
                    "target_source_ids": item.get("target_source_ids") or [],
                    "source_kind": item.get("source_kind"),
                    "authority_rank": item.get("authority_rank"),
                    "scope_replacement": bool(item.get("scope_replacement")),
                    "section_scopes": item.get("section_scopes") or [],
                    "previous_text": str(item.get("previous_text") or "")[
                        : 900 if complex_field else 500
                    ],
                    "effective_text": str(item.get("effective_text") or "")[
                        : 1600 if complex_field else 800
                    ],
                    "required_documents": item.get("required_documents") or [],
                }

            return {
                "chronology_basis": self.chronology_basis,
                "chronology_confidence": (
                    "high" if self.chronology_basis == "complete_revision_sequence" else "medium"
                ),
                "package_amendment_status": package_field_state.get("status"),
                "documents_in_effective_order": sorted(
                    documents.values(),
                    key=lambda item: int(item.get("authority_rank") or 0),
                ),
                "current_source_ids": sorted(current_ids),
                "superseded_source_ids": sorted(deterministic_superseded | historical_ids),
                "controlling_source_ids": sorted(controlling_ids),
                "historical_source_ids": sorted(historical_ids),
                "field_relevant_scopes": sorted(
                    {
                        str(item.get("section_scope") or "")
                        for item in evidence
                        if item.get("source_id") in direct_ids and item.get("section_scope")
                    }
                ),
                "replacement_scopes": sorted(
                    {
                        str(item.get("superseded_by_scope") or item.get("section_scope") or "")
                        for item in evidence
                        if item.get("replacement_scope")
                        or item.get("effective_state") == "superseded"
                    }
                    - {""}
                ),
                "operations": [render_event(item) for item in effective_events],
                "historical_operations": [
                    render_event(item)
                    for item in events
                    if item.get("event_id") not in effective_event_ids
                ],
                "unresolved_conflicts": list(
                    package_field_state.get("unresolved_conflicts") or []
                ),
            }

        current = [
            item
            for item in evidence
            if item.get("effective_state") != "superseded"
            and not item.get("replacement_scope")
        ]
        superseded = [item for item in evidence if item.get("effective_state") == "superseded"]
        direct_ids = {
            str(item.get("source_id") or "")
            for item in evidence
            if field is None
            or item.get("direct_field_match")
            or _direct_field_match(item, field)
        }
        relevant_scopes = {
            str(item.get("section_scope") or "")
            for item in evidence
            if item.get("source_id") in direct_ids and item.get("section_scope")
        }
        direct_clauses = {
            _phrase_text(str(item.get("clause_number") or ""))
            for item in evidence
            if item.get("source_id") in direct_ids and item.get("clause_number")
        }
        operations: list[dict[str, Any]] = []
        seen_operations: set[tuple[str, str, str]] = set()
        for item in current:
            text = str(item.get("text") or "")
            lowered = text.casefold()
            source_id = str(item.get("source_id") or "")
            scope = str(item.get("section_scope") or "")
            normalized_cells = {_phrase_text(str(cell)) for cell in item.get("table_cells") or []}
            if item.get("unit_type") == "table_header" or {
                "existing clause",
                "revised clause",
            }.issubset(normalized_cells):
                continue
            revision = _formal_revision_cells(item)
            if revision:
                if field is not None and source_id not in direct_ids:
                    continue
                operation = "replace"
                previous_text, effective_text = revision
            elif any(cue in lowered for cue in _DELETE_CUES):
                if field is not None and source_id not in direct_ids and scope not in relevant_scopes:
                    continue
                operation = "delete"
                previous_text, effective_text = text, ""
            elif item.get("replacement_scope"):
                if field is not None and scope not in relevant_scopes:
                    continue
                operation = "replace_scope"
                previous_text = ""
                effective_text = next(
                    (
                        str(part)
                        for part in reversed(item.get("section_path") or [])
                        if "revised" in _phrase_text(str(part))
                        or "replacement" in _phrase_text(str(part))
                    ),
                    f"Revised section {item.get('section_scope') or ''}".strip(),
                )
            elif any(cue in lowered for cue in _EXPLICIT_UPDATE_CUES):
                if field is not None and source_id not in direct_ids:
                    continue
                operation = "replace"
                previous_text, effective_text = "", text
            else:
                continue
            identity = (
                (
                    str(item.get("document_id") or "")
                    if operation == "replace_scope"
                    else str(item.get("source_id") or "")
                ),
                operation,
                str(item.get("section_scope") or ""),
            )
            if identity in seen_operations:
                continue
            seen_operations.add(identity)
            operations.append(
                {
                    "operation": operation,
                    "source_id": item.get("source_id"),
                    "document_id": item.get("document_id"),
                    "authority_rank": item.get("authority_rank"),
                    "section_scope": item.get("section_scope") or "",
                    "previous_text": previous_text[:500],
                    "effective_text": effective_text[:500],
                }
            )
        operations.sort(
            key=lambda item: (
                int(item.get("authority_rank") or 0),
                item.get("operation") in {"replace", "delete"},
            ),
            reverse=True,
        )
        controlling_candidates = [
            item
            for item in current
            if item.get("source_id") in direct_ids
            or (
                item.get("replacement_scope")
                and item.get("section_scope") in relevant_scopes
                and (
                    not direct_clauses
                    or _phrase_text(str(item.get("clause_number") or "")) in direct_clauses
                )
            )
        ]
        update_candidates = [
            item
            for item in controlling_candidates
            if item.get("source_role") == "post_base_update"
        ]
        if update_candidates:
            controlling_authority = max(
                int(item.get("authority_rank") or 0) for item in update_candidates
            )
            controlling = [
                item
                for item in update_candidates
                if int(item.get("authority_rank") or 0) == controlling_authority
            ]
            direct_controlling = [
                item for item in controlling if item.get("source_id") in direct_ids
            ]
            if direct_controlling:
                controlling = direct_controlling
        else:
            controlling = controlling_candidates
        controlling_ids = [str(item.get("source_id") or "") for item in controlling]
        controlling_id_set = set(controlling_ids)
        historical_ids = [
            str(item.get("source_id") or "")
            for item in evidence
            if item.get("source_role") == "post_base_update"
            and item.get("source_id") in direct_ids
            and item.get("source_id") not in controlling_id_set
        ]
        if controlling_ids:
            current_operations = [
                item for item in operations if item.get("source_id") in controlling_id_set
            ]
            historical_operations = [
                item for item in operations if item.get("source_id") not in controlling_id_set
            ]
        else:
            current_operations = operations
            historical_operations = []
        documents = {
            str(item.get("document_id") or ""): {
                "document_id": str(item.get("document_id") or ""),
                "source_file": str(item.get("source_file") or ""),
                "source_role": str(item.get("source_role") or "unknown"),
                "revision_sequence": item.get("revision_sequence"),
                "document_order": item.get("document_order"),
                "authority_rank": item.get("authority_rank"),
            }
            for item in evidence
            if item.get("document_id")
        }
        return {
            "chronology_basis": self.chronology_basis,
            "chronology_confidence": (
                "high" if self.chronology_basis == "complete_revision_sequence" else "medium"
            ),
            "documents_in_effective_order": sorted(
                documents.values(),
                key=lambda item: int(item.get("authority_rank") or 0),
            ),
            "current_source_ids": [str(item["source_id"]) for item in current],
            "superseded_source_ids": [str(item["source_id"]) for item in superseded],
            "controlling_source_ids": controlling_ids,
            "historical_source_ids": historical_ids,
            "field_relevant_scopes": sorted(relevant_scopes),
            "replacement_scopes": sorted(
                {
                    str(
                        item.get("superseded_by_scope")
                        or item.get("section_scope")
                        or ""
                    )
                    for item in evidence
                    if item.get("replacement_scope")
                    or item.get("effective_state") == "superseded"
                }
                - {""}
            ),
            "operations": current_operations[:12],
            "historical_operations": historical_operations[:12],
        }


__all__ = ["FieldRetrievalState"]
