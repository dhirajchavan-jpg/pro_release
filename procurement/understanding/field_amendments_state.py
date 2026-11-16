"""Effective-state reduction for normalized amendment events."""

from __future__ import annotations

import re
from typing import Any

from .field_amendments_parsing import _AMENDMENT_SOURCE_KIND_RANK
from .field_core import _phrase_text
from .schema import RfpFieldSpec

def _merge_amendment_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_rank = {
        "unresolved": 0,
        "new_target": 1,
        "explicit_reference": 2,
        "configured_field": 3,
        "linked": 4,
    }
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        identity = (
            event.get("operation"),
            event.get("target_key"),
            tuple(sorted(str(item) for item in event.get("source_ids") or [])),
        )
        current = merged.get(identity)
        candidate_rank = (
            target_rank.get(str(event.get("target_status") or ""), 0),
            len(event.get("target_source_ids") or []),
            float(event.get("confidence") or 0),
            len(str(event.get("effective_text") or "")),
        )
        current_rank = (
            target_rank.get(str(current.get("target_status") or ""), 0),
            len(current.get("target_source_ids") or []),
            float(current.get("confidence") or 0),
            len(str(current.get("effective_text") or "")),
        ) if current else None
        if current is None or candidate_rank > current_rank:
            merged[identity] = event
    return list(merged.values())

def _material_numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<![a-z])\d+(?:\.\d+)?(?:\s*%)?", value.casefold()))

def _reduce_field_amendments(
    field: RfpFieldSpec,
    events: list[dict[str, Any]],
    response_status: str,
    warnings: list[str],
) -> dict[str, Any]:
    ordered = sorted(
        events,
        key=lambda event: (
            int(event.get("authority_rank") or 0),
            _AMENDMENT_SOURCE_KIND_RANK.get(str(event.get("source_kind") or ""), 0),
            int(event.get("source_order") or 0),
        ),
    )
    scalar_field = field.value_type in {
        "boolean",
        "integer",
        "number",
        "money",
        "date",
        "datetime",
    }
    mutations = [
        event
        for event in ordered
        if event["operation"] in {"replace", "delete"}
        or (scalar_field and event["operation"] == "add")
    ]
    current: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    conflicts: list[str] = []
    winners: dict[str, dict[str, Any]] = {}
    mutation_groups: dict[str, list[dict[str, Any]]] = {}
    for event in mutations:
        mutation_groups.setdefault(str(event.get("target_key") or field.key), []).append(event)
    for target_key, target_mutations in mutation_groups.items():
        winning_precedence = max(
            (
                int(event.get("authority_rank") or 0),
                _AMENDMENT_SOURCE_KIND_RANK.get(str(event.get("source_kind") or ""), 0),
            )
            for event in target_mutations
        )
        finalists = [
            event
            for event in target_mutations
            if (
                int(event.get("authority_rank") or 0),
                _AMENDMENT_SOURCE_KIND_RANK.get(str(event.get("source_kind") or ""), 0),
            )
            == winning_precedence
        ]
        conflicting_pair = any(
            (
                left["operation"],
                _phrase_text(str(left.get("effective_text") or "")),
            )
            != (
                right["operation"],
                _phrase_text(str(right.get("effective_text") or "")),
            )
            and not (
                (left_sources := set(left.get("source_ids") or []))
                and (right_sources := set(right.get("source_ids") or []))
                and (left_sources.issubset(right_sources) or right_sources.issubset(left_sources))
            )
            for index, left in enumerate(finalists)
            for right in finalists[index + 1 :]
        )
        if conflicting_pair:
            conflicts.append(
                f"Equal-authority amendment events disagree on target {target_key}."
            )
        winner = max(finalists, key=lambda event: int(event.get("source_order") or 0))
        winners[target_key] = winner
        current.append(winner)
        historical.extend(
            event
            for event in target_mutations
            if event["event_id"] != winner["event_id"]
        )
    for event in ordered:
        if event in mutations:
            continue
        winner = winners.get(str(event.get("target_key") or field.key))
        if event["operation"] == "add":
            if winner and int(event.get("authority_rank") or 0) < int(winner.get("authority_rank") or 0):
                historical.append(event)
            else:
                current.append(event)
            continue
        if event["operation"] == "clarify" and winner:
            event_numbers = _material_numbers(str(event.get("effective_text") or ""))
            winner_numbers = _material_numbers(str(winner.get("effective_text") or ""))
            lower_same_authority = (
                int(event.get("authority_rank") or 0) == int(winner.get("authority_rank") or 0)
                and _AMENDMENT_SOURCE_KIND_RANK.get(str(event.get("source_kind") or ""), 0)
                < _AMENDMENT_SOURCE_KIND_RANK.get(str(winner.get("source_kind") or ""), 0)
            )
            if lower_same_authority and event_numbers and winner_numbers and event_numbers != winner_numbers:
                historical.append(event)
                continue
        if not winner or int(event.get("authority_rank") or 0) >= int(winner.get("authority_rank") or 0):
            current.append(event)
        else:
            historical.append(event)
    scope_deletes = [
        event
        for event in current
        if event.get("operation") == "delete" and event.get("scope_replacement")
    ]
    if scope_deletes:
        controlling_scope_delete = max(
            scope_deletes,
            key=lambda event: (
                int(event.get("authority_rank") or 0),
                int(event.get("source_order") or 0),
            ),
        )
        controlling_rank = int(controlling_scope_delete.get("authority_rank") or 0)
        for event in list(current):
            if (
                event is not controlling_scope_delete
                and int(event.get("authority_rank") or 0) < controlling_rank
            ):
                current.remove(event)
                historical.append(event)
    scope_replacements = [
        event
        for event in current
        if event.get("operation") == "replace" and event.get("scope_replacement")
    ]
    for replacement in scope_replacements:
        replacement_rank = int(replacement.get("authority_rank") or 0)
        replacement_numbers = _material_numbers(
            str(replacement.get("effective_text") or "")
        )
        if not replacement_numbers:
            continue
        for event in list(current):
            event_numbers = _material_numbers(str(event.get("effective_text") or ""))
            if (
                event is not replacement
                and event.get("operation") == "clarify"
                and int(event.get("authority_rank") or 0) < replacement_rank
                and event_numbers
                and event_numbers != replacement_numbers
            ):
                current.remove(event)
                historical.append(event)
    unresolved_targets = [
        event["event_id"]
        for event in current
        if event.get("target_status") == "unresolved"
        and event.get("operation") in {"replace", "delete", "retain", "clarify"}
    ]
    if unresolved_targets:
        conflicts.append("An effective amendment could not be linked to a base clause or explicit target.")
    current_ids = {event["event_id"] for event in current}
    historical_ids = {event["event_id"] for event in historical}
    rendered_events = []
    for event in ordered:
        copy = dict(event)
        copy["effective_status"] = (
            "effective"
            if event["event_id"] in current_ids
            else "superseded"
            if event["event_id"] in historical_ids
            else "pending"
        )
        rendered_events.append(copy)
    status = (
        "unresolved"
        if response_status == "unresolved" or conflicts
        else "resolved"
        if rendered_events
        else "no_change"
    )
    return {
        "field_key": field.key,
        "status": status,
        "events": rendered_events,
        "effective_event_ids": [event["event_id"] for event in current],
        "historical_event_ids": [event["event_id"] for event in historical],
        "current_source_ids": list(
            dict.fromkeys(source_id for event in current for source_id in event["source_ids"])
        ),
        "historical_source_ids": list(
            dict.fromkeys(
                source_id
                for event in historical
                for source_id in event["source_ids"]
            )
        ),
        "target_source_ids": list(
            dict.fromkeys(source_id for event in current for source_id in event["target_source_ids"])
        ),
        "unresolved_conflicts": conflicts,
        "warnings": warnings,
    }
