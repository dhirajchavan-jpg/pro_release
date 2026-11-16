"""Reviewed Base RFP amendment application and audit history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from hashlib import sha256
from typing import Any

from pydantic import ValidationError

from .base_rfp_models import BaseRfpAmendmentEvent, BaseRfpUnderstanding, _amendment_datetime
from .base_rfp_normalization import _canonical, _count_source_refs


class _AmendmentTargetError(ValueError):
    pass


_MISSING = object()


def _amendment_path(path: str) -> list[str]:
    if path.startswith("/"):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    else:
        parts = path.split(".")
    if not parts or any(not part for part in parts):
        raise _AmendmentTargetError("target_path must be a non-empty dotted path or JSON pointer")
    return parts


def _target_ids(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [
        item
        for key, item in value.items()
        if (key == "id" or key.endswith("_id")) and isinstance(item, str) and item
    ]


def _target_text_matches(value: Any, target_text: str) -> bool:
    if isinstance(value, str):
        return value == target_text
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(value.get(key), str) and value[key] == target_text
        for key in ("text", "value", "title", "name", "description", "definition")
    )


def _unique_target_index(items: list[Any], event: BaseRfpAmendmentEvent) -> int:
    if not event.target_id and not event.target_text:
        raise _AmendmentTargetError("a list target requires target_id or target_text")
    matches = [
        index
        for index, item in enumerate(items)
        if (not event.target_id or event.target_id in _target_ids(item))
        and (not event.target_text or _target_text_matches(item, event.target_text))
    ]
    if len(matches) != 1:
        raise _AmendmentTargetError(
            f"target matched {len(matches)} items; exactly one is required"
        )
    return matches[0]


def _resolve_existing_target(
    root: dict[str, Any], event: BaseRfpAmendmentEvent
) -> tuple[dict[str, Any] | list[Any], str | int, Any, dict[str, Any] | None, str]:
    current: Any = root
    parent: dict[str, Any] | list[Any] | None = None
    key: str | int | None = None
    provenance_owner: dict[str, Any] | None = None
    selector_used = False
    selected_locator = ""

    for token in _amendment_path(event.target_path):
        if isinstance(current, list):
            if token.isdecimal():
                index = int(token)
                if index >= len(current):
                    raise _AmendmentTargetError(f"list index {index} is out of range")
                item = current[index]
                if event.target_id and not selector_used:
                    if event.target_id not in _target_ids(item):
                        raise _AmendmentTargetError("target_id does not match the indexed item")
                    selector_used = True
            else:
                index = _unique_target_index(current, event)
                item = current[index]
                selector_used = True
            parent, key, current = current, index, item
            selected_locator = next(iter(_target_ids(item)), str(index))
            if isinstance(current, dict) and "source_refs" in current:
                provenance_owner = current
            if token.isdecimal():
                continue

        if not isinstance(current, dict):
            raise _AmendmentTargetError(f"target_path cannot traverse {token!r}")
        if "source_refs" in current:
            provenance_owner = current
        if token not in current:
            raise _AmendmentTargetError(f"target_path component {token!r} does not exist")
        parent, key, current = current, token, current[token]

    if isinstance(current, list) and (event.target_id or event.target_text):
        index = _unique_target_index(current, event)
        item = current[index]
        parent, key, current = current, index, item
        selector_used = True
        selected_locator = next(iter(_target_ids(item)), str(index))
    if isinstance(current, dict) and "source_refs" in current:
        provenance_owner = current
    if event.target_id and not selector_used:
        if event.target_id not in _target_ids(current):
            raise _AmendmentTargetError("target_id does not identify the resolved target")
        selected_locator = event.target_id
    if event.target_text and not _target_text_matches(current, event.target_text):
        raise _AmendmentTargetError("target_text does not exactly match the resolved target")
    if parent is None or key is None:
        raise _AmendmentTargetError("target_path cannot replace the document root")

    locator = f"{event.target_path}#{event.target_id or selected_locator}" if event.target_id or selected_locator else event.target_path
    return parent, key, current, provenance_owner, locator


def _resolve_add_target(
    root: dict[str, Any], event: BaseRfpAmendmentEvent
) -> tuple[dict[str, Any] | list[Any], str | int, Any, dict[str, Any] | None, bool]:
    current: Any = root
    parent: dict[str, Any] | list[Any] | None = None
    key: str | int | None = None
    provenance_owner: dict[str, Any] | None = None
    parts = _amendment_path(event.target_path)

    for position, token in enumerate(parts):
        if isinstance(current, list):
            if not token.isdecimal():
                raise _AmendmentTargetError("add paths crossing a list must use an explicit index")
            index = int(token)
            if index >= len(current):
                raise _AmendmentTargetError(f"list index {index} is out of range")
            parent, key, current = current, index, current[index]
            if isinstance(current, dict) and "source_refs" in current:
                provenance_owner = current
            continue
        if not isinstance(current, dict):
            raise _AmendmentTargetError(f"target_path cannot traverse {token!r}")
        if "source_refs" in current:
            provenance_owner = current
        if token not in current:
            if position != len(parts) - 1:
                raise _AmendmentTargetError(f"target_path component {token!r} does not exist")
            return current, token, _MISSING, provenance_owner, True
        parent, key, current = current, token, current[token]

    if parent is None or key is None:
        raise _AmendmentTargetError("target_path cannot add at the document root")
    return parent, key, current, provenance_owner, False


def _event_source_refs(event: BaseRfpAmendmentEvent) -> list[dict[str, Any]]:
    return [ref.model_dump(mode="json") for ref in event.source_refs]


def _merge_source_refs(target: Any, refs: list[dict[str, Any]], *, create: bool = False) -> None:
    if not isinstance(target, dict) or ("source_refs" not in target and not create):
        return
    existing = target.get("source_refs") if isinstance(target.get("source_refs"), list) else []
    target["source_refs"] = list(
        {_canonical(ref): ref for ref in [*existing, *refs]}.values()
    )


def _apply_replace(root: dict[str, Any], event: BaseRfpAmendmentEvent) -> str:
    parent, key, current, owner, locator = _resolve_existing_target(root, event)
    replacement = deepcopy(event.value)
    refs = _event_source_refs(event)

    if isinstance(current, dict) and isinstance(replacement, dict):
        for id_key, id_value in replacement.items():
            if (
                (id_key == "id" or id_key.endswith("_id"))
                and current.get(id_key)
                and id_value != current[id_key]
            ):
                raise _AmendmentTargetError(f"replacement cannot change {id_key}")
        old_refs = current.get("source_refs") if isinstance(current.get("source_refs"), list) else []
        new_refs = replacement.get("source_refs") if isinstance(replacement.get("source_refs"), list) else []
        updated = {**current, **replacement}
        if "source_refs" in current or "source_refs" in replacement:
            updated["source_refs"] = []
            _merge_source_refs(updated, [*old_refs, *new_refs, *refs])
        parent[key] = updated
    elif isinstance(current, dict):
        matches = [
            field
            for field in ("text", "value")
            if event.target_text and current.get(field) == event.target_text
        ]
        if len(matches) != 1:
            raise _AmendmentTargetError(
                "a scalar replacement of an object requires target_text to identify text or value"
            )
        current[matches[0]] = replacement
        _merge_source_refs(current, refs)
    else:
        parent[key] = replacement
        _merge_source_refs(owner, refs)
    return locator


def _apply_delete(root: dict[str, Any], event: BaseRfpAmendmentEvent) -> str:
    parent, key, current, owner, locator = _resolve_existing_target(root, event)
    if isinstance(parent, list):
        parent.pop(int(key))
    else:
        del parent[str(key)]
    if owner is not current:
        _merge_source_refs(owner, _event_source_refs(event))
    return locator


def _apply_add(root: dict[str, Any], event: BaseRfpAmendmentEvent) -> str:
    parent, key, current, owner, missing = _resolve_add_target(root, event)
    addition = deepcopy(event.value)
    refs = _event_source_refs(event)

    if missing:
        if event.target_id:
            raise _AmendmentTargetError("target_id is only valid when adding to a list")
        if event.target_text and not _target_text_matches(addition, event.target_text):
            raise _AmendmentTargetError("target_text does not exactly match the added value")
        parent[key] = addition
        _merge_source_refs(owner, refs)
        return event.target_path

    if isinstance(current, list):
        addition_ids = _target_ids(addition)
        if event.target_id and event.target_id not in addition_ids:
            raise _AmendmentTargetError("target_id must match an ID in the added value")
        existing_ids = {target_id for item in current for target_id in _target_ids(item)}
        if existing_ids.intersection(addition_ids):
            raise _AmendmentTargetError("the added value duplicates an existing target ID")
        if event.target_text and not _target_text_matches(addition, event.target_text):
            raise _AmendmentTargetError("target_text does not exactly match the added value")
        if isinstance(addition, dict) and _amendment_path(event.target_path)[-1] in {
            "clauses", "requirements", "facts", "events"
        }:
            _merge_source_refs(addition, refs, create=True)
        current.append(addition)
        locator = event.target_id or next(iter(addition_ids), str(len(current) - 1))
        return f"{event.target_path}#{locator}"

    if isinstance(current, dict) and isinstance(addition, dict):
        collisions = sorted(current.keys() & addition.keys())
        if collisions:
            raise _AmendmentTargetError(f"add would overwrite existing keys: {', '.join(collisions)}")
        current.update(addition)
        _merge_source_refs(owner or current, refs)
        return event.target_path
    raise _AmendmentTargetError("add target must be a list, object, or missing final path")


def _apply_reviewed_amendment(root: dict[str, Any], event: BaseRfpAmendmentEvent) -> str:
    if event.operation == "add":
        return _apply_add(root, event)
    if event.operation == "delete":
        return _apply_delete(root, event)
    return _apply_replace(root, event)


def _audit_entry(
    event: BaseRfpAmendmentEvent,
    status: Literal["applied", "skipped", "superseded"],
    *,
    reason: str = "",
    resolved_path: str = "",
) -> dict[str, Any]:
    return {
        "event": event.model_dump(mode="json"),
        "status": status,
        "reason": reason,
        "resolved_path": resolved_path,
        "superseded_by_event_id": "",
    }


def _record_skipped_amendment(data: dict[str, Any], event: BaseRfpAmendmentEvent, reason: str) -> None:
    data["audit"]["amendment_history"].append(_audit_entry(event, "skipped", reason=reason))
    warning = f"Amendment {event.event_id} skipped: {reason}"
    if warning not in data["audit"]["warnings"]:
        data["audit"]["warnings"].append(warning)


def _mark_superseded_amendments(
    history: list[dict[str, Any]], event: BaseRfpAmendmentEvent, resolved_path: str
) -> None:
    explicit = set(event.supersedes_event_ids)
    for entry in history:
        previous_id = str(entry.get("event", {}).get("event_id") or "")
        same_target = event.operation != "add" and entry.get("resolved_path") == resolved_path
        if entry.get("status") == "applied" and (previous_id in explicit or same_target):
            entry["status"] = "superseded"
            entry["superseded_by_event_id"] = event.event_id


def _validation_reason(exc: ValidationError) -> str:
    error = exc.errors(include_url=False)[0]
    path = ".".join(str(part) for part in error["loc"])
    return f"result is invalid at {path}: {error['msg']}"


def apply_base_rfp_amendments(
    base: Mapping[str, Any] | BaseRfpUnderstanding,
    events: Iterable[Mapping[str, Any] | BaseRfpAmendmentEvent] | Mapping[str, Any] | BaseRfpAmendmentEvent,
) -> BaseRfpUnderstanding:
    """Apply effective reviewed events to a copy, retaining every decision in audit history."""
    model = base if isinstance(base, BaseRfpUnderstanding) else BaseRfpUnderstanding.model_validate(base)
    if isinstance(events, (Mapping, BaseRfpAmendmentEvent)):
        events = [events]

    ordered: list[tuple[int, BaseRfpAmendmentEvent]] = []
    event_ids: set[str] = set()
    for input_order, raw_event in enumerate(events):
        event = raw_event if isinstance(raw_event, BaseRfpAmendmentEvent) else BaseRfpAmendmentEvent.model_validate(raw_event)
        if event.event_id in event_ids:
            raise ValueError(f"duplicate amendment event_id: {event.event_id}")
        event_ids.add(event.event_id)
        ordered.append((input_order, event))
    ordered.sort(
        key=lambda item: (
            _amendment_datetime(item[1].effective_at or item[1].issued_at),
            item[0],
        )
    )

    data = deepcopy(model.model_dump(mode="json"))
    applied = False
    for _, event in ordered:
        _merge_source_refs(data["audit"], _event_source_refs(event))
        if event.resolution_status not in {"reviewed", "resolved"}:
            _record_skipped_amendment(
                data, event, f"resolution_status is {event.resolution_status}"
            )
            continue
        if event.effective_status != "effective":
            _record_skipped_amendment(
                data, event, f"effective_status is {event.effective_status}"
            )
            continue

        candidate = deepcopy(data)
        try:
            resolved_path = _apply_reviewed_amendment(candidate, event)
            BaseRfpUnderstanding.model_validate(candidate)
        except _AmendmentTargetError as exc:
            _record_skipped_amendment(data, event, str(exc))
            continue
        except ValidationError as exc:
            _record_skipped_amendment(data, event, _validation_reason(exc))
            continue

        data = candidate
        history = data["audit"]["amendment_history"]
        _mark_superseded_amendments(history, event, resolved_path)
        history.append(_audit_entry(event, "applied", resolved_path=resolved_path))
        applied = True

    if applied:
        if model.record_id:
            data["audit"]["input_record_ids"] = sorted(
                {*data["audit"]["input_record_ids"], model.record_id}
            )
        data["coverage"]["documents_total"] = len(data["source_documents"])
        data["coverage"]["clauses_total"] = len(data["clauses"])
        data["coverage"]["requirements_total"] = len(data["requirements"])
        data["coverage"]["source_refs_total"] = _count_source_refs(data)
        payload = {key: value for key, value in data.items() if key not in {"record_id", "audit"}}
        digest = sha256(_canonical(payload).encode()).hexdigest()
        data["record_id"] = f"rfp_{digest[:16]}"
        data["audit"]["content_sha256"] = digest
    return BaseRfpUnderstanding.model_validate(data)


__all__ = [
    "apply_base_rfp_amendments",
]

