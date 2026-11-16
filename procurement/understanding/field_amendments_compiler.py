"""Package-level amendment compilation and review orchestration."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable

from .field_amendments_parsing import (
    _deterministic_amendment_events,
    _normalize_amendment_response,
    _residual_amendment_evidence,
    amendment_response_schema,
)
from .field_amendments_state import (
    _merge_amendment_events,
    _reduce_field_amendments,
)
from .field_core import (
    AMENDMENT_COMPILER_VERSION,
    _automatic_amendment_visual_page_id,
    _compact_text,
    _hard_provider_block_reason,
    _phrase_text,
    _pointer_only_amendment_response,
    _source_digest,
    _unaccounted_binding_amendment_source_ids,
    _unit_document,
    _unit_text,
    _write_json_atomic,
)
from .field_retrieval import LocalFieldRetriever
from .schema import (
    RfpFieldSpec,
    RfpSectionSpec,
    RfpUnderstandingSchema,
    schema_field_count,
)

def _amendment_cache_path(
    checkpoint_dir: Path,
    source_digest: str,
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    model_tag: str,
    image_enabled: bool,
) -> Path:
    fingerprint = sha256(
        json.dumps(
            {
                "compiler": AMENDMENT_COMPILER_VERSION,
                "source": source_digest,
                "schema_id": schema.schema_id,
                "section": {
                    "key": section.key,
                    "label": section.label,
                    "prompt": section.prompt,
                },
                "field": field.model_dump(mode="json"),
                "model": model_tag,
                "image_enabled": image_enabled,
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return checkpoint_dir / f"{field.key[:32]}-{fingerprint[:20]}.json"

def _amendment_payload(
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    retriever: LocalFieldRetriever,
    *,
    prior_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task": f"compile_rfp_amendments_{field.key}",
        "schema_id": schema.schema_id,
        "field": field.model_dump(mode="json"),
        "section": {
            "key": section.key,
            "label": section.label,
            "prompt": section.prompt,
        },
        "documents_in_order": sorted(
            (
                {
                    "document_id": document,
                    "authority_rank": rank,
                    "revision_sequence": next(
                        (
                            unit.get("revision_sequence")
                            for unit in retriever.units.values()
                            if _unit_document(unit) == document
                        ),
                        None,
                    ),
                }
                for document, rank in retriever.document_authority.items()
            ),
            key=lambda item: item["authority_rank"],
        ),
        "rules": [
            "Compile amendment events for this one configured field; do not answer the field yet.",
            "Use no_change when the supplied update evidence contains no binding change for this field. Use unresolved only for a real unresolved reference, conflict, or incomplete amendment.",
            "Use only supplied source IDs. A bidder query is never binding by itself.",
            "In existing-versus-revised tables, existing_text is historical and revised_text is effective.",
            "For parallel_nested_columns, the extractor already split the old and revised nested tables; compare existing_text with revised_text and emit the binding change.",
            "For paired_nested_rows, the extractor joined the old row, revised row, and any next-page continuation; treat the complete revised_text as one criterion.",
            "For implicit_parallel_columns, repeated table halves were detected as old and revised versions even though OCR lost their labels; compare existing_text with revised_text.",
            "An embedded_revision_fragment is a nested cell extraction, never a standalone amendment; interpret it only with the main formal_revision_scope rows on that page.",
            "A revised nested table may continue in the next formal_revision_scope row; do not select the detached old nested table when the main row supplies both sides.",
            "Classify each binding change as add, replace, delete, retain, or clarify.",
            "When a revised list retains some members and omits others, use replace with the complete retained list or an atomic delete for only the omitted member; never represent the whole configured field as deleted.",
            "As per RFP/RFE means retain. Refer corrigendum is unresolved only when the actual revised clause is absent from both evidence and prior_response; otherwise it adds no new change.",
            "A later formal corrigendum outranks an older clarification or bidder request.",
            "Keep required proof documents attached to the same amended criterion.",
            "A keyword inside the old or revised clause does not prove relevance: the row's target, clause, or particulars must concern this field.",
            "Return at most six atomic events. Keep each previous_text and effective_text under 100 words while preserving every material number, condition, exception, and proof document; summarize boilerplate instead of copying it.",
            "Request at most two page images only when flattened table text cannot establish the operation.",
            "If the actual revised clause is missing, return unresolved and provide a search_query of at most 12 words that can retrieve it. Otherwise leave search_query empty.",
            "For a tentative no_change result, always provide one residual search_query using alternative procurement wording so absence can be audited once.",
            "Return unresolved instead of guessing a target, operation, or effective value.",
        ],
        "evidence": [
            {
                key: item.get(key)
                for key in (
                    "source_id",
                    "document_id",
                    "page",
                    "page_id",
                    "unit_type",
                    "section_path",
                    "clause_number",
                    "source_role",
                    "revision_sequence",
                    "authority_rank",
                    "formal_revision",
                    "formal_revision_scope",
                    "embedded_revision_fragment",
                    "amendment_candidate",
                    "direct_field_match",
                    "anchor_match",
                    "revision_layout",
                    "revision_source_ids",
                    "table_headers",
                    "table_cells",
                    "existing_text",
                    "revised_text",
                    "issuer_response",
                    "bidder_request",
                    "table_continuation_source_ids",
                    "table_continuation_text",
                    "text",
                )
                if item.get(key) not in (None, "", [])
            }
            for item in evidence
        ],
        "prior_response": prior_response or {},
    }
def _amendment_evidence_batches(
    evidence: list[dict[str, Any]],
    *,
    max_chars: int = 9000,
) -> list[list[dict[str, Any]]]:
    updates = [item for item in evidence if item.get("source_role") == "post_base_update"]
    aligned_updates = [
        item
        for item in updates
        if item.get("amendment_candidate")
        or item.get("direct_field_match")
        or item.get("anchor_match")
        or item.get("formal_revision")
        or item.get("formal_revision_scope")
        or item.get("replacement_scope")
    ]
    if aligned_updates:
        updates = aligned_updates
    source_order = {
        str(item.get("source_id") or ""): index
        for index, item in enumerate(evidence)
    }
    core = [
        item
        for item in updates
        if item.get("formal_revision_scope")
        and item.get("direct_field_match")
        and not item.get("embedded_revision_fragment")
    ]
    core_ids = {str(item.get("source_id") or "") for item in core}
    rank = lambda item: (
        -int(item.get("authority_rank") or 0),
        source_order.get(str(item.get("source_id") or ""), 0),
    )
    ordered_updates = [
        *sorted(core, key=rank),
        *sorted(
            [item for item in updates if item.get("source_id") not in core_ids],
            key=rank,
        ),
    ]
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used_chars = 0
    in_core = True
    for item in ordered_updates:
        item_is_core = str(item.get("source_id") or "") in core_ids
        size = (
            len(str(item.get("text") or ""))
            + len(str(item.get("table_continuation_text") or ""))
            + 200
        )
        if current and (
            (in_core and not item_is_core)
            or used_chars + size > max(2000, max_chars)
        ):
            batches.append(current)
            current = []
            used_chars = 0
        in_core = item_is_core
        current.append(item)
        used_chars += size
    if current:
        batches.append(current)
    return batches

def compile_package_amendments(
    schema: RfpUnderstandingSchema,
    retriever: LocalFieldRetriever,
    *,
    call: Callable[..., dict[str, Any]] | None = None,
    checkpoint_dir: Path | None = None,
    image_loader: Callable[[str], dict[str, str] | None] | None = None,
    model_tag: str = "",
    evidence_chars: int = 18000,
    max_calls: int = 24,
    max_images: int = 2,
    max_image_followup_calls: int = 8,
    max_output_tokens: int = 1200,
    deterministic_short_circuit: bool = False,
) -> dict[str, Any]:
    """Compile each configured field's update chain once for reuse by final projection."""

    source_digest = _source_digest(retriever.units)
    if retriever.chronology_basis == "unresolved_revision_order":
        fields = {
            field.key: _reduce_field_amendments(
                field,
                [],
                "unresolved",
                [
                    "Amendment order is ambiguous; formal changes were not applied automatically.",
                    *retriever.chronology_warnings,
                ],
            )
            for section in schema.sections
            for field in section.fields
        }
        return {
            "version": AMENDMENT_COMPILER_VERSION,
            "status": "partial",
            "fields": fields,
            "unresolved_fields": list(fields),
            "not_compiled_fields": [],
            "audit": {
                "model_calls": 0,
                "standard_model_calls": 0,
                "cache_hits": 0,
                "chronology_basis": retriever.chronology_basis,
                "chronology_warnings": list(retriever.chronology_warnings),
                "fault_isolated": True,
            },
        }
    if not any(
        unit.get("source_role") == "post_base_update"
        for unit in retriever.units.values()
    ):
        fields = {
            field.key: _reduce_field_amendments(field, [], "no_change", [])
            for section in schema.sections
            for field in section.fields
        }
        return {
            "version": AMENDMENT_COMPILER_VERSION,
            "status": "complete",
            "fields": fields,
            "unresolved_fields": [],
            "not_compiled_fields": [],
            "audit": {
                "model_calls": 0,
                "standard_model_calls": 0,
                "cache_hits": 0,
                "max_calls": max(0, max_calls),
                "max_image_followup_calls": max(0, max_image_followup_calls),
                "configured_fields": schema_field_count(schema),
                "image_requests": 0,
                "automatic_image_verifications": 0,
                "images_sent": 0,
                "image_followup_calls": 0,
                "image_requested_page_ids": [],
                "automatic_image_page_ids": [],
                "image_sent_page_ids": [],
                "provider_blocked": False,
                "provider_blocked_reason": "",
            },
        }
    fields: dict[str, dict[str, Any]] = {}
    standard_calls_made = 0
    cache_hits = 0
    provider_blocked_reason = ""
    image_request_page_ids: list[str] = []
    automatic_image_page_ids: list[str] = []
    image_sent_page_ids: list[str] = []
    image_followup_calls = 0

    def amendment_failure(
        field: RfpFieldSpec,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = _reduce_field_amendments(
            field,
            [],
            "unresolved",
            [f"Amendment processing needs human review: {_compact_text(reason)[:500]}"],
        )
        state["evidence_source_ids"] = [
            str(item.get("source_id") or "")
            for item in evidence or []
            if item.get("source_id")
        ]
        state["audit"] = {
            "fault_isolated": True,
            "error_code": "amendment_field_error",
            "error_detail": _compact_text(reason)[:700],
        }
        return state

    for section in schema.sections:
        for field in section.fields:
            cache_path = (
                _amendment_cache_path(
                    checkpoint_dir,
                    source_digest,
                    schema,
                    section,
                    field,
                    model_tag,
                    image_loader is not None,
                )
                if checkpoint_dir
                else None
            )
            if cache_path and cache_path.is_file():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    if cached.get("field_key") == field.key and cached.get("status") in {
                        "resolved",
                        "no_change",
                    }:
                        fields[field.key] = cached
                        cache_hits += 1
                        continue
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            try:
                evidence = retriever.retrieve_amendment_evidence(
                    schema,
                    section,
                    field,
                    max_chars=max(4000, evidence_chars),
                )
            except Exception as exc:
                fields[field.key] = amendment_failure(field, str(exc))
                continue
            updates = [item for item in evidence if item.get("source_role") == "post_base_update"]
            if not updates:
                fields[field.key] = _reduce_field_amendments(field, [], "no_change", [])
                continue
            try:
                deterministic_events, deterministic_warnings = _deterministic_amendment_events(
                    field,
                    evidence,
                    retriever,
                )
            except Exception as exc:
                fields[field.key] = amendment_failure(field, str(exc), evidence)
                continue
            audit_evidence = list(evidence)
            if deterministic_short_circuit:
                evidence = _residual_amendment_evidence(
                    field,
                    evidence,
                    deterministic_events,
                    retriever,
                )
                updates = [
                    item
                    for item in evidence
                    if item.get("source_role") == "post_base_update"
                ]
                if not updates:
                    state = _reduce_field_amendments(
                        field,
                        deterministic_events,
                        "resolved" if deterministic_events else "no_change",
                        deterministic_warnings,
                    )
                    state["evidence_source_ids"] = [
                        str(item.get("source_id") or "")
                        for item in audit_evidence
                        if item.get("source_id")
                    ]
                    fields[field.key] = state
                    if cache_path and state["status"] in {"resolved", "no_change"}:
                        try:
                            _write_json_atomic(cache_path, state)
                        except Exception:
                            pass
                    continue
            if call is None and deterministic_short_circuit:
                state = _reduce_field_amendments(
                    field,
                    deterministic_events,
                    "unresolved",
                    [
                        *deterministic_warnings,
                        "Substantive amendment rows remain after deterministic compilation; model review was skipped.",
                    ],
                )
                state["evidence_source_ids"] = [
                    str(item.get("source_id") or "")
                    for item in audit_evidence
                    if item.get("source_id")
                ]
                fields[field.key] = state
                continue
            if call is None:
                fields[field.key] = {
                    **_reduce_field_amendments(field, [], "no_change", []),
                    "status": "not_compiled",
                }
                continue
            if provider_blocked_reason:
                state = _reduce_field_amendments(
                    field,
                    deterministic_events,
                    "unresolved",
                    [
                        *deterministic_warnings,
                        "Amendment model review was not attempted because the provider "
                        f"is unavailable: {provider_blocked_reason}",
                    ],
                )
                state["evidence_source_ids"] = [
                    str(item.get("source_id") or "")
                    for item in audit_evidence
                    if item.get("source_id")
                ]
                fields[field.key] = state
                continue
            if standard_calls_made >= max(0, max_calls):
                state = _reduce_field_amendments(
                    field,
                    deterministic_events,
                    "resolved" if deterministic_events else "unresolved",
                    [
                        *deterministic_warnings,
                        (
                            "The call budget was exhausted after the controlling revised rows were compiled deterministically."
                            if deterministic_events
                            else "Package amendment call budget was exhausted."
                        ),
                    ],
                )
                state["evidence_source_ids"] = [
                    str(item.get("source_id") or "")
                    for item in audit_evidence
                    if item.get("source_id")
                ]
                fields[field.key] = state
                if cache_path and state["status"] in {"resolved", "no_change"}:
                    try:
                        _write_json_atomic(cache_path, state)
                    except Exception:
                        pass
                continue
            complex_field = field.value_type in {
                "string",
                "string_array",
                "object",
                "object_array",
            }
            prebatch = complex_field and len(updates) >= 8
            if prebatch:
                raw = {
                    "status": "unresolved",
                    "events": [],
                    "image_page_ids": [],
                    "search_query": "",
                    "notes": "",
                }
                events: list[dict[str, Any]] = []
                response_status = "unresolved"
                image_page_ids: list[str] = []
                warnings: list[str] = []
                was_truncated = True
            else:
                prior_response = None
                if deterministic_short_circuit:
                    prior_response = {
                        "status": "partially_compiled",
                        "deterministic_events": [
                            {
                                "operation": event.get("operation"),
                                "target": event.get("target"),
                                "effective_text": str(event.get("effective_text") or "")[:400],
                                "source_ids": event.get("source_ids") or [],
                            }
                            for event in deterministic_events
                        ],
                    }
                payload = _amendment_payload(
                    schema,
                    section,
                    field,
                    evidence,
                    retriever,
                    prior_response=prior_response,
                )
                primary_output_tokens = max(512, max_output_tokens)
                standard_calls_made += 1
                try:
                    raw = call(
                        system=(
                            "You compile source-grounded RFP corrigendum events. Return concise JSON only; "
                            "never copy a bidder request as an issuer amendment."
                        ),
                        payload=payload,
                        response_schema=amendment_response_schema(),
                        media_parts=[],
                        max_output_tokens=primary_output_tokens,
                        review=False,
                        allow_truncation_retry=False,
                    )
                except Exception as exc:
                    provider_blocked_reason = (
                        _hard_provider_block_reason(exc) or provider_blocked_reason
                    )
                    state = _reduce_field_amendments(
                        field,
                        deterministic_events,
                        "unresolved",
                        [
                            *deterministic_warnings,
                            f"Amendment compiler call failed: {_compact_text(str(exc))[:240]}",
                        ],
                    )
                    state["evidence_source_ids"] = [
                        str(item.get("source_id") or "")
                        for item in audit_evidence
                        if item.get("source_id")
                    ]
                    fields[field.key] = state
                    continue
                if not isinstance(raw, dict):
                    raw = {
                        "status": "unresolved",
                        "events": [],
                        "image_page_ids": [],
                        "search_query": "",
                        "notes": "",
                    }
                try:
                    events, response_status, image_page_ids, warnings = (
                        _normalize_amendment_response(raw, field, evidence, retriever)
                    )
                except Exception as exc:
                    fields[field.key] = amendment_failure(field, str(exc), evidence)
                    continue
                image_request_page_ids.extend(image_page_ids)
                was_truncated = "truncat" in _phrase_text(
                    " ".join([str(raw.get("notes") or ""), *warnings])
                )
            if was_truncated:
                seed_events = list(events)
                batch_events: list[dict[str, Any]] = list(seed_events)
                batch_failed = False
                unvalidated_core_batches: list[tuple[int, set[str]]] = []
                validated_event_source_ids = {
                    str(source_id)
                    for event in [*deterministic_events, *seed_events]
                    for source_id in event.get("source_ids") or []
                }
                evidence_batches = _amendment_evidence_batches(evidence)
                batch_cursor = 0
                while batch_cursor < len(evidence_batches):
                    batch_index = batch_cursor + 1
                    batch = evidence_batches[batch_cursor]
                    batch_cursor += 1
                    batch = [
                        item
                        for item in batch
                        if str(item.get("source_id") or "")
                        not in validated_event_source_ids
                    ]
                    if not batch:
                        continue
                    batch_source_ids = {
                        str(item.get("source_id") or "") for item in batch
                    }
                    batch_is_core = bool(batch) and all(
                        item.get("formal_revision_scope")
                        and item.get("amendment_candidate")
                        and not item.get("embedded_revision_fragment")
                        for item in batch
                    )
                    prior_events = [
                        event
                        for event in [*deterministic_events, *batch_events]
                        if not (
                            batch_is_core
                            and batch_source_ids.intersection(event.get("source_ids") or [])
                        )
                    ]
                    if standard_calls_made >= max(0, max_calls):
                        warnings.append(
                            "Package amendment call budget was exhausted before all truncated evidence batches were processed."
                        )
                        batch_failed = True
                        break
                    batch_payload = _amendment_payload(
                        schema,
                        section,
                        field,
                        batch,
                        retriever,
                        prior_response={
                            "status": "partially_compiled",
                            "batch": batch_index,
                            "batch_count": len(evidence_batches),
                            "events_from_prior_batches": [
                                {
                                    "operation": event.get("operation"),
                                    "target": event.get("target"),
                                    "effective_text": str(
                                        event.get("effective_text") or ""
                                    )[:400],
                                    "source_ids": event.get("source_ids") or [],
                                }
                                for event in prior_events[-6:]
                            ],
                        },
                    )
                    batch_payload["rules"].append(
                        "This is one exhaustive evidence batch. Return at most three concise events from this batch only."
                    )
                    batch_payload["rules"].append(
                        "Prior-batch events are context: do not repeat them, and use no_change when this batch merely repeats or refers to an already supplied revision."
                    )
                    if prior_events:
                        batch_payload["rules"].append(
                            "This is a residual audit against prior-batch events. Return any distinct binding add, replacement, deletion, retention, or material clarification not already represented. Chronology is reduced later: preserve an earlier compatible clarification unless a later source actually contradicts it."
                        )
                    elif batch_is_core:
                        batch_payload["rules"].append(
                            "This formal row is one field-level amendment: return one event, keeping OR alternatives and their proof documents inside that event."
                        )
                    standard_calls_made += 1
                    try:
                        batch_output_tokens = min(1400, max(512, max_output_tokens))
                        if complex_field:
                            batch_output_tokens = min(
                                2200,
                                max(1800, max_output_tokens),
                            )
                        batch_raw = call(
                            system=(
                                "You classify only binding RFP amendment events in one bounded evidence batch. "
                                + (
                                    "Extract distinct compatible events from this batch without repeating supplied prior events; return at most three new events. "
                                    if prior_events
                                    else "Compile the core formal revision as one field-level event. "
                                )
                                + "Use no_change, not unresolved, when this batch contains no new binding event."
                            ),
                            payload=batch_payload,
                            response_schema=amendment_response_schema(),
                            media_parts=[],
                            max_output_tokens=batch_output_tokens,
                            review=True,
                            allow_truncation_retry=False,
                        )
                    except Exception as exc:
                        provider_blocked_reason = (
                            _hard_provider_block_reason(exc) or provider_blocked_reason
                        )
                        warnings.append(
                            f"Amendment evidence batch {batch_index} failed: {str(exc)[:240]}"
                        )
                        batch_failed = True
                        if provider_blocked_reason:
                            break
                        continue
                    if not isinstance(batch_raw, dict):
                        warnings.append(
                            f"Amendment evidence batch {batch_index} returned no structured result."
                        )
                        batch_failed = True
                        continue
                    try:
                        normalized, batch_status, batch_images, batch_warnings = (
                            _normalize_amendment_response(
                                batch_raw,
                                field,
                                evidence,
                                retriever,
                            )
                        )
                    except Exception as exc:
                        warnings.append(
                            f"Amendment evidence batch {batch_index} could not be normalized: {str(exc)[:240]}"
                        )
                        batch_failed = True
                        continue
                    image_page_ids.extend(batch_images)
                    image_request_page_ids.extend(batch_images)
                    batch_note = _phrase_text(str(batch_raw.get("notes") or ""))
                    known_formal_revision = any(
                        event.get("source_kind") == "formal_revision"
                        for event in prior_events
                    )
                    if (
                        batch_status == "unresolved"
                        and known_formal_revision
                        and "corrigendum" in batch_note
                        and any(
                            cue in batch_note
                            for cue in ("refer", "actual revised", "revised text", "missing")
                        )
                    ):
                        batch_status = "no_change"
                        batch_warnings = [
                            warning
                            for warning in batch_warnings
                            if not (
                                batch_note.startswith(_phrase_text(warning))
                                or _phrase_text(warning).startswith(batch_note)
                            )
                        ]
                        batch_warnings.append(
                            "Residual corrigendum reference was satisfied by the already compiled formal revision."
                        )
                    batch_was_truncated = "truncat" in _phrase_text(
                        " ".join(
                            [str(batch_raw.get("notes") or ""), *batch_warnings]
                        )
                    )
                    batch_events.extend(normalized)
                    validated_event_source_ids.update(
                        str(source_id)
                        for event in normalized
                        for source_id in event.get("source_ids") or []
                    )
                    if batch_was_truncated:
                        remaining = [
                            item
                            for item in batch
                            if str(item.get("source_id") or "")
                            not in validated_event_source_ids
                        ]
                        if remaining and len(batch) > 1:
                            midpoint = max(1, len(remaining) // 2)
                            retries = [remaining[:midpoint], remaining[midpoint:]]
                            evidence_batches[batch_cursor:batch_cursor] = [
                                retry for retry in retries if retry
                            ]
                            continue
                        if remaining:
                            warnings.extend(batch_warnings)
                            batch_failed = True
                        continue
                    warnings.extend(batch_warnings)
                    explicit_core_change = any(
                        item.get("existing_text")
                        and item.get("revised_text")
                        and _phrase_text(str(item.get("existing_text")))
                        != _phrase_text(str(item.get("revised_text")))
                        for item in batch
                    )
                    core_requires_event = (
                        field.value_type
                        in {"boolean", "integer", "number", "money", "date", "datetime"}
                        or explicit_core_change
                    )
                    if (
                        batch_is_core
                        and core_requires_event
                        and batch_status == "no_change"
                        and not normalized
                        and not validated_event_source_ids.intersection(batch_source_ids)
                    ):
                        unvalidated_core_batches.append((batch_index, batch_source_ids))
                    if batch_status == "unresolved":
                        batch_failed = True
                validated_source_ids = {
                    str(source_id)
                    for event in [*deterministic_events, *batch_events]
                    for source_id in event.get("source_ids") or []
                }
                for batch_index, batch_source_ids in unvalidated_core_batches:
                    if validated_source_ids.intersection(batch_source_ids):
                        continue
                    warnings.append(
                        f"Formal revision batch {batch_index} returned no_change without a validated old-versus-revised event."
                    )
                    batch_failed = True
                events = _merge_amendment_events(batch_events)
                response_status = (
                    "unresolved"
                    if batch_failed
                    else "resolved"
                    if events
                    else "no_change"
                )
                image_page_ids = list(dict.fromkeys(image_page_ids))
            if deterministic_events:
                events = _merge_amendment_events([*deterministic_events, *events])
                if response_status == "no_change":
                    response_status = "resolved"
            warnings = [*deterministic_warnings, *warnings]
            completeness_gap_ids = _unaccounted_binding_amendment_source_ids(
                evidence,
                events,
            )
            if completeness_gap_ids:
                response_status = "unresolved"
                warnings.append(
                    "Substantive binding amendment rows remain unaccounted for: "
                    + ", ".join(completeness_gap_ids[:6])
                )
            latest_binding_authority = max(
                (
                    int(item.get("authority_rank") or 0)
                    for item in evidence
                    if item.get("source_role") == "post_base_update"
                    and item.get("effective_state") != "superseded"
                    and not _pointer_only_amendment_response(_unit_text(item))
                    and (item.get("direct_field_match") or item.get("anchor_match"))
                ),
                default=0,
            )
            compiled_authority = max(
                (int(event.get("authority_rank") or 0) for event in events),
                default=0,
            )
            authority_gap = bool(
                events
                and latest_binding_authority > compiled_authority
            )
            if authority_gap:
                response_status = "unresolved"
                warnings.append(
                    "A higher-authority aligned amendment remains unaccounted for; the older event cannot control this field."
                )
            search_query = " ".join(
                _compact_text(raw.get("search_query")).split()[:12]
            )
            if (authority_gap or completeness_gap_ids) and not search_query:
                search_query = " ".join(
                    _compact_text(f"{field.label} latest binding amendment").split()[:12]
                )
            if (
                not image_page_ids
                and response_status in {"unresolved", "no_change"}
                and image_loader is not None
                and max_images > 0
                and image_followup_calls < max(0, max_image_followup_calls)
            ):
                automatic_page_id = _automatic_amendment_visual_page_id(evidence)
                if automatic_page_id:
                    image_page_ids = [automatic_page_id]
                    automatic_image_page_ids.append(automatic_page_id)
            should_retry = (
                bool(image_page_ids) or not was_truncated
            ) and (
                response_status == "unresolved"
                or (
                    response_status == "no_change"
                    and bool(search_query or image_page_ids)
                )
            )
            image_reserve_available = (
                bool(image_page_ids)
                and image_loader is not None
                and max_images > 0
                and image_followup_calls < max(0, max_image_followup_calls)
            )
            standard_retry_available = standard_calls_made < max(0, max_calls)
            if should_retry and (image_reserve_available or standard_retry_available):
                if search_query:
                    try:
                        expanded_evidence = retriever.retrieve_amendment_evidence(
                            schema,
                            section,
                            field,
                            max_chars=max(4000, evidence_chars),
                            extra_query=search_query,
                        )
                        if expanded_evidence:
                            audit_evidence = expanded_evidence
                            evidence = expanded_evidence
                            deterministic_events, expanded_warnings = (
                                _deterministic_amendment_events(field, evidence, retriever)
                            )
                            warnings.extend(expanded_warnings)
                    except Exception as exc:
                        warnings.append(
                            f"Amendment evidence expansion needs review: {str(exc)[:240]}"
                        )
                media_parts: list[dict[str, Any]] = []
                loaded_image_page_ids: list[str] = []
                if image_reserve_available:
                    for page_id in image_page_ids[: max(1, max_images)]:
                        try:
                            image = image_loader(page_id)
                        except Exception as exc:
                            warnings.append(
                                f"Amendment page image {page_id} could not be loaded: {str(exc)[:180]}"
                            )
                            continue
                        if not image or not image.get("data") or not image.get("mime_type"):
                            continue
                        media_parts.extend(
                            [
                                {"text": f"AMENDMENT_PAGE_IMAGE page_id={page_id}"},
                                {
                                    "inline_data": {
                                        "mime_type": image["mime_type"],
                                        "data": image["data"],
                                    }
                                }
                            ]
                        )
                        loaded_image_page_ids.append(page_id)
                can_review = bool(media_parts) or (
                    bool(search_query) and standard_retry_available
                )
                if can_review:
                    if media_parts:
                        image_followup_calls += 1
                        image_sent_page_ids.extend(loaded_image_page_ids)
                    else:
                        standard_calls_made += 1
                    try:
                        reviewed = call(
                            system=(
                                "You verify one unresolved RFP amendment from newly retrieved exact text and any requested page images. "
                                "Return the complete corrected event set: preserve valid prior events and add every omitted binding event. "
                                "Return concise JSON only."
                            ),
                            payload=_amendment_payload(
                                schema,
                                section,
                                field,
                                evidence,
                                retriever,
                                prior_response=raw,
                            ),
                            response_schema=amendment_response_schema(),
                            media_parts=media_parts,
                            max_output_tokens=max(512, max_output_tokens),
                            review=True,
                            allow_truncation_retry=False,
                        )
                        if isinstance(reviewed, dict):
                            events, response_status, _images, review_warnings = (
                                _normalize_amendment_response(reviewed, field, evidence, retriever)
                            )
                            if deterministic_events:
                                events = _merge_amendment_events(
                                    [*deterministic_events, *events]
                                )
                                if response_status == "no_change":
                                    response_status = "resolved"
                            warnings.extend(review_warnings)
                    except Exception as exc:
                        provider_blocked_reason = (
                            _hard_provider_block_reason(exc) or provider_blocked_reason
                        )
                        warnings.append(f"Amendment evidence review failed: {str(exc)[:240]}")
            final_completeness_gap_ids = _unaccounted_binding_amendment_source_ids(
                evidence,
                events,
            )
            if final_completeness_gap_ids:
                response_status = "unresolved"
                final_warning = (
                    "Substantive binding amendment rows remain unaccounted for: "
                    + ", ".join(final_completeness_gap_ids[:6])
                )
                if final_warning not in warnings:
                    warnings.append(final_warning)
            state = _reduce_field_amendments(field, events, response_status, warnings)
            state["evidence_source_ids"] = [
                str(item.get("source_id") or "")
                for item in audit_evidence
                if item.get("source_id")
            ]
            fields[field.key] = state
            if cache_path and state["status"] in {"resolved", "no_change"}:
                try:
                    _write_json_atomic(cache_path, state)
                except Exception:
                    pass
    unresolved_fields = [
        key for key, state in fields.items() if state.get("status") == "unresolved"
    ]
    not_compiled_fields = [
        key for key, state in fields.items() if state.get("status") == "not_compiled"
    ]
    return {
        "version": AMENDMENT_COMPILER_VERSION,
        "status": (
            "partial"
            if unresolved_fields
            else "not_run"
            if not_compiled_fields
            else "complete"
        ),
        "fields": fields,
        "unresolved_fields": unresolved_fields,
        "not_compiled_fields": not_compiled_fields,
        "audit": {
            "model_calls": standard_calls_made + image_followup_calls,
            "standard_model_calls": standard_calls_made,
            "cache_hits": cache_hits,
            "max_calls": max(0, max_calls),
            "max_image_followup_calls": max(0, max_image_followup_calls),
            "configured_fields": schema_field_count(schema),
            "image_requests": len(image_request_page_ids),
            "automatic_image_verifications": len(
                set(automatic_image_page_ids) & set(image_sent_page_ids)
            ),
            "images_sent": len(image_sent_page_ids),
            "image_followup_calls": image_followup_calls,
            "image_requested_page_ids": list(dict.fromkeys(image_request_page_ids)),
            "automatic_image_page_ids": list(
                dict.fromkeys(
                    page_id
                    for page_id in automatic_image_page_ids
                    if page_id in image_sent_page_ids
                )
            ),
            "image_sent_page_ids": list(dict.fromkeys(image_sent_page_ids)),
            "provider_blocked": bool(provider_blocked_reason),
            "provider_blocked_reason": provider_blocked_reason,
        },
    }
