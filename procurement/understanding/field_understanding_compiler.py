"""Field-understanding compiler implementation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable

from .field_core import (
    AMENDMENT_COMPILER_VERSION,
    COMPILER_VERSION,
    OUTPUT_SCHEMA_VERSION,
    _CACHEABLE_STATUSES,
    _amendment_state_source_ids,
    _automatic_visual_page_id,
    _compact_projection_response_schema,
    _compact_text,
    _contains_phrase,
    _field_cache_path,
    _field_output_budget,
    _hard_provider_block_reason,
    _is_placeholder_value,
    _phrase_text,
    _prefer_more_complete_found,
    _project_grounded_identifier,
    _project_unique_labeled_value,
    _proof_repair_response_schema,
    _requires_exhaustive_coverage,
    _source_digest,
    _write_json_atomic,
    field_response_schema,
)
from .field_retrieval import LocalFieldRetriever
from .field_amendments import _reduce_field_amendments, compile_package_amendments
from .field_prompts import (
    _compact_projection_payload,
    _field_payload,
    _merge_proof_repair,
    _merge_visual_verification,
    _proof_repair_payload,
    _targeted_visual_verification_payload,
)
from .field_results import (
    _blocking_grounding_issues,
    _completeness_issues,
    _completeness_query,
    _deterministic_scalar_result,
    _drop_serialized_collection_artifacts,
    _explicit_not_applicable_value,
    _money_value_grounded,
    _normalize_result,
    _project_controlling_scalar,
    _project_controlling_total_marks,
    _project_currency_markers,
    _project_deleted_members,
    _project_explicit_formulas,
    _project_prompt_exclusions,
    _project_template_conflict,
    _promote_explicit_exceptions,
    _request_details,
)
from .schema import (
    RfpFieldSpec,
    RfpSectionSpec,
    RfpUnderstandingSchema,
    schema_field_count,
)
from .field_understanding_batch import _initial_field_batch_responses
from .field_understanding_field import _resolve_field
from .field_understanding_state import (
    _SharedCallBudget,
    _SharedProviderBlock,
    _failed_understanding_result,
    _unresolved_field_result,
)

def compile_field_understanding(
    schema: RfpUnderstandingSchema,
    source_units: dict[str, dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Compile fields without allowing a field-local anomaly to escape."""
    try:
        return _compile_field_understanding_impl(schema, source_units, **kwargs)
    except Exception as exc:
        return _failed_understanding_result(schema, source_units, str(exc))


def _compile_field_understanding_impl(
    schema: RfpUnderstandingSchema,
    source_units: dict[str, dict[str, Any]],
    *,
    call: Callable[..., dict[str, Any]],
    checkpoint_dir: Path | None = None,
    image_loader: Callable[[str], dict[str, str] | None] | None = None,
    model_tag: str = "",
    initial_candidates: int = 8,
    expanded_candidates: int = 16,
    initial_evidence_chars: int = 9000,
    expanded_evidence_chars: int = 16000,
    max_rounds: int = 3,
    max_images_per_field: int = 3,
    max_image_followup_calls: int = 8,
    max_output_tokens: int = 1536,
    max_total_calls: int | None = None,
    amendment_call: Callable[..., dict[str, Any]] | None = None,
    amendment_checkpoint_dir: Path | None = None,
    amendment_evidence_chars: int = 18000,
    max_amendment_calls: int = 24,
    max_amendment_images: int = 2,
    amendment_output_tokens: int = 1200,
    amendment_deterministic_short_circuit: bool = False,
    field_concurrency: int = 1,
    semantic_ranker: Callable[[str, list[tuple[str, str]]], list[str]] | None = None,
    semantic_pool: int = 32,
    _precompiled_amendment_state: dict[str, Any] | None = None,
    _standard_budget: _SharedCallBudget | None = None,
    _image_budget: _SharedCallBudget | None = None,
    _provider_block: _SharedProviderBlock | None = None,
    _initial_responses: dict[str, dict[str, Any]] | None = None,
    _batch_audit: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Resolve configured fields with bounded, adaptive evidence requests."""

    retriever = LocalFieldRetriever(
        source_units,
        semantic_ranker=semantic_ranker,
        semantic_pool=semantic_pool,
    )
    digest = _source_digest(retriever.units)
    total_fields = schema_field_count(schema)
    call_limit = (
        max_total_calls
        if max_total_calls is not None
        else total_fields * max(1, max_rounds)
    )
    standard_calls_made = 0
    cache_hits = 0
    provider_blocked_reason = ""
    sections_out: list[dict[str, Any]] = []
    if _precompiled_amendment_state is not None:
        amendment_state = _precompiled_amendment_state
    else:
        try:
            amendment_state = compile_package_amendments(
                schema,
                retriever,
                call=amendment_call,
                checkpoint_dir=amendment_checkpoint_dir,
                image_loader=image_loader,
                model_tag=model_tag,
                evidence_chars=max(4000, amendment_evidence_chars),
                max_calls=max(0, max_amendment_calls),
                max_images=max(0, max_amendment_images),
                max_image_followup_calls=max(0, max_image_followup_calls),
                max_output_tokens=max(512, amendment_output_tokens),
                deterministic_short_circuit=amendment_deterministic_short_circuit,
            )
        except Exception as exc:
            amendment_reason = _compact_text(str(exc))[:700]
            amendment_fields: dict[str, dict[str, Any]] = {}
            for amendment_section in schema.sections:
                for amendment_field in amendment_section.fields:
                    state = _reduce_field_amendments(
                        amendment_field,
                        [],
                        "unresolved",
                        [
                            "Amendment processing needs human review: "
                            + amendment_reason
                        ],
                    )
                    state["audit"] = {
                        "fault_isolated": True,
                        "error_code": "amendment_compilation_boundary_error",
                        "error_detail": amendment_reason,
                    }
                    amendment_fields[amendment_field.key] = state
            amendment_state = {
                "version": AMENDMENT_COMPILER_VERSION,
                "status": "partial",
                "fields": amendment_fields,
                "unresolved_fields": list(amendment_fields),
                "not_compiled_fields": [],
                "audit": {
                    "fault_isolated": True,
                    "error_code": "amendment_compilation_boundary_error",
                    "error_detail": amendment_reason,
                    "provider_blocked": False,
                    "provider_blocked_reason": "",
                },
            }
    provider_blocked_reason = _compact_text(
        amendment_state.get("audit", {}).get("provider_blocked_reason")
    )
    field_image_call_limit = max(
        0,
        max_image_followup_calls
        - int(amendment_state.get("audit", {}).get("image_followup_calls") or 0),
    )
    field_image_calls_made = 0
    standard_budget = _standard_budget or _SharedCallBudget(call_limit)
    image_budget = _image_budget or _SharedCallBudget(field_image_call_limit)
    provider_block = _provider_block or _SharedProviderBlock(provider_blocked_reason)
    initial_responses = _initial_responses or {}
    batch_audit = _batch_audit or {
        "model_calls": 0,
        "standard_model_calls": 0,
        "image_model_calls": 0,
    }

    if total_fields > 1 and _standard_budget is None:
        work_items = [
            (index, section, field)
            for index, (section, field) in enumerate(
                (section, field)
                for section in schema.sections
                for field in section.fields
            )
        ]
        initial_responses, batch_audit = _initial_field_batch_responses(
            schema,
            retriever,
            work_items,
            amendment_state,
            call=call,
            checkpoint_dir=checkpoint_dir,
            image_loader=image_loader,
            model_tag=model_tag,
            initial_candidates=initial_candidates,
            initial_evidence_chars=initial_evidence_chars,
            max_images_per_field=max_images_per_field,
            max_output_tokens=max_output_tokens,
            field_concurrency=field_concurrency,
            standard_budget=standard_budget,
            image_budget=image_budget,
            provider_block=provider_block,
        )

        def compile_one(
            item: tuple[int, RfpSectionSpec, RfpFieldSpec],
        ) -> tuple[int, dict[str, Any]]:
            index, section, field = item
            try:
                field_schema = schema.model_copy(
                    update={
                        "sections": [section.model_copy(update={"fields": [field]})]
                    }
                )
                return index, compile_field_understanding(
                    field_schema,
                    source_units,
                    call=call,
                    checkpoint_dir=checkpoint_dir,
                    image_loader=image_loader,
                    model_tag=model_tag,
                    initial_candidates=initial_candidates,
                    expanded_candidates=expanded_candidates,
                    initial_evidence_chars=initial_evidence_chars,
                    expanded_evidence_chars=expanded_evidence_chars,
                    max_rounds=max_rounds,
                    max_images_per_field=max_images_per_field,
                    max_image_followup_calls=max_image_followup_calls,
                    max_output_tokens=max_output_tokens,
                    max_total_calls=call_limit,
                    amendment_call=None,
                    amendment_checkpoint_dir=amendment_checkpoint_dir,
                    amendment_evidence_chars=amendment_evidence_chars,
                    max_amendment_calls=max_amendment_calls,
                    max_amendment_images=max_amendment_images,
                    amendment_output_tokens=amendment_output_tokens,
                    amendment_deterministic_short_circuit=amendment_deterministic_short_circuit,
                    field_concurrency=1,
                    semantic_ranker=semantic_ranker,
                    semantic_pool=semantic_pool,
                    _precompiled_amendment_state=amendment_state,
                    _standard_budget=standard_budget,
                    _image_budget=image_budget,
                    _provider_block=provider_block,
                    _initial_responses=initial_responses,
                    _batch_audit=batch_audit,
                )
            except Exception as exc:
                return index, {
                    "sections": [
                        {
                            "key": section.key,
                            "label": section.label,
                            "fields": [_unresolved_field_result(field, str(exc))],
                        }
                    ],
                    "audit": {
                        "fault_isolated": True,
                        "error_code": "field_shard_error",
                        "error_detail": _compact_text(str(exc))[:700],
                    },
                }

        shard_results: dict[int, dict[str, Any]] = {}
        workers = min(max(1, field_concurrency), len(work_items))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(compile_one, item) for item in work_items]
            for future in as_completed(futures):
                index, compiled = future.result()
                shard_results[index] = compiled

        ordered_fields: list[dict[str, Any]] = []
        shard_audits: list[dict[str, Any]] = []
        for index in range(len(work_items)):
            compiled = shard_results[index]
            shard_audits.append(compiled["audit"])
            ordered_fields.append(compiled["sections"][0]["fields"][0])
        field_cursor = 0
        sections_out = []
        for section in schema.sections:
            section_field_count = len(section.fields)
            sections_out.append(
                {
                    "key": section.key,
                    "label": section.label,
                    "fields": ordered_fields[
                        field_cursor : field_cursor + section_field_count
                    ],
                }
            )
            field_cursor += section_field_count
        all_fields = [field for section in sections_out for field in section["fields"]]
        unresolved = sum(
            field["status"] in {"ambiguous", "unresolved"} for field in all_fields
        )
        unverified = sum(
            field.get("audit", {}).get("verified") is not True for field in all_fields
        )
        found = sum(field["status"] == "found" for field in all_fields)
        status = (
            "complete"
            if unresolved == 0 and unverified == 0
            else "partial"
            if found
            else "failed"
        )
        provider_blocked_reason = provider_block.get()
        return {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "schema_id": schema.schema_id,
            "source_digest": digest,
            "status": status,
            "amendment_state": amendment_state,
            "sections": sections_out,
            "audit": {
                "compiler_version": COMPILER_VERSION,
                "source_units": len(retriever.units),
                "fields_total": len(all_fields),
                "fields_found": found,
                "fields_not_found": sum(
                    field["status"] == "not_found" for field in all_fields
                ),
                "fields_not_applicable": sum(
                    field["status"] == "not_applicable" for field in all_fields
                ),
                "fields_ambiguous": sum(
                    field["status"] == "ambiguous" for field in all_fields
                ),
                "fields_need_more_evidence": sum(
                    field["status"] == "need_more_evidence" for field in all_fields
                ),
                "fields_unresolved": sum(
                    field["status"] == "unresolved" for field in all_fields
                ),
                "fields_unverified": unverified,
                "model_calls": int(batch_audit.get("model_calls") or 0) + sum(
                    int(audit.get("model_calls") or 0) for audit in shard_audits
                ),
                "standard_model_calls": int(
                    batch_audit.get("standard_model_calls") or 0
                ) + sum(
                    int(audit.get("standard_model_calls") or 0)
                    for audit in shard_audits
                ),
                "image_model_calls": int(
                    batch_audit.get("image_model_calls") or 0
                ) + sum(
                    int(audit.get("image_model_calls") or 0)
                    for audit in shard_audits
                ),
                "batch_model_calls": int(batch_audit.get("model_calls") or 0),
                "field_image_requests": sum(
                    int(audit.get("field_image_requests") or 0)
                    for audit in shard_audits
                ),
                "field_images_sent": sum(
                    int(audit.get("field_images_sent") or 0)
                    for audit in shard_audits
                ),
                "field_image_followup_calls": sum(
                    int(audit.get("field_image_followup_calls") or 0)
                    for audit in shard_audits
                ),
                "field_automatic_image_verifications": sum(
                    int(audit.get("field_automatic_image_verifications") or 0)
                    for audit in shard_audits
                ),
                "amendment_model_calls": amendment_state.get("audit", {}).get(
                    "model_calls", 0
                ),
                "amendment_image_requests": amendment_state.get("audit", {}).get(
                    "image_requests", 0
                ),
                "amendment_automatic_image_verifications": amendment_state.get(
                    "audit", {}
                ).get("automatic_image_verifications", 0),
                "amendment_images_sent": amendment_state.get("audit", {}).get(
                    "images_sent", 0
                ),
                "amendment_image_followup_calls": amendment_state.get(
                    "audit", {}
                ).get("image_followup_calls", 0),
                "amendment_cache_hits": amendment_state.get("audit", {}).get(
                    "cache_hits", 0
                ),
                "cache_hits": sum(
                    int(audit.get("cache_hits") or 0) for audit in shard_audits
                ),
                "call_limit": call_limit,
                "field_concurrency": workers,
                "image_followup_call_limit": max(0, max_image_followup_calls),
                "image_followup_calls_total": image_budget.used
                + int(
                    amendment_state.get("audit", {}).get("image_followup_calls")
                    or 0
                ),
                "provider_blocked": bool(provider_blocked_reason),
                "provider_blocked_reason": provider_blocked_reason,
            },
        }

    for section in schema.sections:
        fields_out: list[dict[str, Any]] = []
        for field in section.fields:
            field_result, field_audit = _resolve_field(
                schema,
                retriever,
                section,
                field,
                digest=digest,
                call=call,
                checkpoint_dir=checkpoint_dir,
                image_loader=image_loader,
                model_tag=model_tag,
                initial_candidates=initial_candidates,
                expanded_candidates=expanded_candidates,
                initial_evidence_chars=initial_evidence_chars,
                expanded_evidence_chars=expanded_evidence_chars,
                max_rounds=max_rounds,
                max_images_per_field=max_images_per_field,
                max_output_tokens=max_output_tokens,
                amendment_deterministic_short_circuit=amendment_deterministic_short_circuit,
                amendment_state=amendment_state,
                standard_budget=standard_budget,
                image_budget=image_budget,
                provider_block=provider_block,
                initial_responses=initial_responses,
            )
            fields_out.append(field_result)
            cache_hits += int(field_audit.get("cache_hits") or 0)
            standard_calls_made += int(
                field_audit.get("standard_model_calls") or 0
            )
            field_image_calls_made += int(
                field_audit.get("image_model_calls") or 0
            )
        sections_out.append(
            {
                "key": section.key,
                "label": section.label,
                "fields": fields_out,
            }
        )

    all_fields = [field for section in sections_out for field in section["fields"]]
    unresolved = sum(field["status"] in {"ambiguous", "unresolved"} for field in all_fields)
    unverified = sum(field.get("audit", {}).get("verified") is not True for field in all_fields)
    found = sum(field["status"] == "found" for field in all_fields)
    field_image_requests = sum(
        int(field.get("audit", {}).get("image_requests") or 0)
        for field in all_fields
    )
    field_images_sent = sum(
        len(field.get("audit", {}).get("image_sent_page_ids") or [])
        for field in all_fields
    )
    field_image_followup_calls = sum(
        int(field.get("audit", {}).get("image_followup_calls") or 0)
        for field in all_fields
    )
    field_automatic_image_verifications = sum(
        int(field.get("audit", {}).get("automatic_image_verifications") or 0)
        for field in all_fields
    )
    status = "complete" if unresolved == 0 and unverified == 0 else "partial" if found else "failed"
    provider_blocked_reason = provider_block.get()
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "schema_id": schema.schema_id,
        "source_digest": digest,
        "status": status,
        "amendment_state": amendment_state,
        "sections": sections_out,
        "audit": {
            "compiler_version": COMPILER_VERSION,
            "source_units": len(retriever.units),
            "fields_total": len(all_fields),
            "fields_found": found,
            "fields_not_found": sum(field["status"] == "not_found" for field in all_fields),
            "fields_not_applicable": sum(
                field["status"] == "not_applicable" for field in all_fields
            ),
            "fields_ambiguous": sum(field["status"] == "ambiguous" for field in all_fields),
            "fields_need_more_evidence": sum(
                field["status"] == "need_more_evidence" for field in all_fields
            ),
            "fields_unresolved": sum(field["status"] == "unresolved" for field in all_fields),
            "fields_unverified": unverified,
            "model_calls": standard_calls_made + field_image_calls_made,
            "standard_model_calls": standard_calls_made,
            "image_model_calls": field_image_calls_made,
            "field_image_requests": field_image_requests,
            "field_images_sent": field_images_sent,
            "field_image_followup_calls": field_image_followup_calls,
            "field_automatic_image_verifications": (
                field_automatic_image_verifications
            ),
            "amendment_model_calls": amendment_state.get("audit", {}).get("model_calls", 0),
            "amendment_image_requests": amendment_state.get("audit", {}).get(
                "image_requests", 0
            ),
            "amendment_automatic_image_verifications": amendment_state.get(
                "audit", {}
            ).get("automatic_image_verifications", 0),
            "amendment_images_sent": amendment_state.get("audit", {}).get(
                "images_sent", 0
            ),
            "amendment_image_followup_calls": amendment_state.get("audit", {}).get(
                "image_followup_calls", 0
            ),
            "amendment_cache_hits": amendment_state.get("audit", {}).get("cache_hits", 0),
            "cache_hits": cache_hits,
            "call_limit": call_limit,
            "field_concurrency": 1,
            "image_followup_call_limit": max(0, max_image_followup_calls),
            "image_followup_calls_total": (
                field_image_calls_made
                + int(
                    amendment_state.get("audit", {}).get("image_followup_calls") or 0
                )
            ),
            "provider_blocked": bool(provider_blocked_reason),
            "provider_blocked_reason": provider_blocked_reason,
        },
    }
