"""Single-field resolution state machine for tender understanding."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .field_core import (
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
    _write_json_atomic,
    field_response_schema,
)
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
from .field_retrieval import LocalFieldRetriever
from .schema import RfpFieldSpec, RfpSectionSpec, RfpUnderstandingSchema
from .field_understanding_state import (
    _SharedCallBudget,
    _SharedProviderBlock,
)


def _resolve_field(
    schema: RfpUnderstandingSchema,
    retriever: LocalFieldRetriever,
    section: RfpSectionSpec,
    target_field: RfpFieldSpec,
    *,
    digest: str,
    call: Callable[..., dict[str, Any]],
    checkpoint_dir: Path | None,
    image_loader: Callable[[str], dict[str, str] | None] | None,
    model_tag: str,
    initial_candidates: int,
    expanded_candidates: int,
    initial_evidence_chars: int,
    expanded_evidence_chars: int,
    max_rounds: int,
    max_images_per_field: int,
    max_output_tokens: int,
    amendment_deterministic_short_circuit: bool,
    amendment_state: dict[str, Any],
    standard_budget: _SharedCallBudget,
    image_budget: _SharedCallBudget,
    provider_block: _SharedProviderBlock,
    initial_responses: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Resolve one field and return its result plus local audit counters."""

    field_results: list[dict[str, Any]] = []
    cache_hits = 0
    standard_calls_made = 0
    field_image_calls_made = 0
    for field in (target_field,):
        provider_blocked_reason = provider_block.get()
        package_field_state = dict(
            amendment_state.get("fields", {}).get(field.key) or {}
        )
        amendment_source_ids = _amendment_state_source_ids(package_field_state)
        cache_path = (
            _field_cache_path(
                checkpoint_dir,
                digest,
                schema,
                section,
                field,
                model_tag,
                package_field_state,
            )
            if checkpoint_dir
            else None
        )
        if cache_path and cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached.get("key") == field.key
                    and cached.get("status") in _CACHEABLE_STATUSES
                    and not _is_placeholder_value(cached.get("value"))
                    and cached.get("audit", {}).get("verified") is True
                ):
                    cached_audit = cached.setdefault("audit", {})
                    cached_audit["cache_hit"] = True
                    cached_audit["image_requests"] = 0
                    cached_audit["image_requested_page_ids"] = []
                    cached_audit["image_sent_page_ids"] = []
                    cached_audit["image_followup_calls"] = 0
                    cached_audit["automatic_image_verifications"] = 0
                    cached_audit["automatic_image_page_ids"] = []
                    field_results.append(cached)
                    cache_hits += 1
                    continue
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        if package_field_state.get("status") == "unresolved":
            conflicts = list(package_field_state.get("unresolved_conflicts") or [])
            warnings = list(package_field_state.get("warnings") or [])
            field_results.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "status": "ambiguous",
                    "value": None,
                    "conditions": [],
                    "exceptions": list(dict.fromkeys([*conflicts, *warnings]))[:12],
                    "required_documents": [],
                    "evidence": [],
                    "confidence": 0.0,
                    "notes": "The controlling corrigendum state is unresolved; no effective value was guessed.",
                    "audit": {
                        "rounds": 0,
                        "cache_hit": False,
                        "verified": False,
                        "package_amendment_status": "unresolved",
                        "effective_state": retriever.compile_effective_state(
                            [], field, package_field_state
                        ),
                    },
                }
            )
            continue

        evidence = retriever.retrieve(
            schema,
            section,
            field,
            limit=max(1, initial_candidates),
            max_chars=max(1000, initial_evidence_chars),
            requested_source_ids=amendment_source_ids,
        )
        effective_state = retriever.compile_effective_state(
            evidence,
            field,
            package_field_state,
        )
        deterministic_result = _deterministic_scalar_result(
            field,
            effective_state,
            retriever.units,
        )
        deterministic_issues: list[str] = []
        if deterministic_result is not None:
            deterministic_issues = _completeness_issues(
                field,
                deterministic_result,
                evidence,
                effective_state,
            )
            if not deterministic_issues:
                deterministic_result["audit"] = {
                    "rounds": 0,
                    "cache_hit": False,
                    "initial_candidate_count": len(evidence),
                    "final_candidate_count": len(evidence),
                    "completeness_reviewed": True,
                    "verified": True,
                    "completeness_warnings": [],
                    "deterministic_projection": True,
                    "effective_state": effective_state,
                }
                if cache_path:
                    _write_json_atomic(cache_path, deterministic_result)
                field_results.append(deterministic_result)
                continue
        if provider_blocked_reason:
            if deterministic_result is not None:
                deterministic_result["audit"] = {
                    "rounds": 0,
                    "cache_hit": False,
                    "initial_candidate_count": len(evidence),
                    "final_candidate_count": len(evidence),
                    "completeness_reviewed": True,
                    "verified": False,
                    "completeness_warnings": list(
                        dict.fromkeys(
                            [
                                *deterministic_issues,
                                "Model verification was unavailable: "
                                f"{provider_blocked_reason}",
                            ]
                        )
                    ),
                    "deterministic_projection": True,
                    "provider_blocked": True,
                    "effective_state": effective_state,
                }
                field_results.append(deterministic_result)
            else:
                field_results.append(
                    {
                        "key": field.key,
                        "label": field.label,
                        "status": "unresolved",
                        "value": None,
                        "conditions": [],
                        "exceptions": [],
                        "required_documents": [],
                        "evidence": [],
                        "confidence": 0.0,
                        "notes": (
                            "Focused understanding was not attempted because the model "
                            f"provider is unavailable: {provider_blocked_reason}"
                        )[:900],
                        "audit": {
                            "rounds": 0,
                            "cache_hit": False,
                            "verified": False,
                            "provider_blocked": True,
                        },
                    }
                )
            continue
        if not evidence:
            field_results.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "status": "unresolved",
                    "value": None,
                    "conditions": [],
                    "exceptions": [],
                    "required_documents": [],
                    "evidence": [],
                    "confidence": 0.0,
                    "notes": "No relevant source candidates were retrieved; absence was not inferred.",
                    "audit": {
                        "rounds": 0,
                        "cache_hit": False,
                        "verified": False,
                        "no_retrieval_candidates": True,
                    },
                }
            )
            continue
        if not standard_budget.available():
            if deterministic_result is not None:
                deterministic_result["audit"] = {
                    "rounds": 0,
                    "cache_hit": False,
                    "initial_candidate_count": len(evidence),
                    "final_candidate_count": len(evidence),
                    "completeness_reviewed": True,
                    "verified": False,
                    "completeness_warnings": deterministic_issues,
                    "deterministic_projection": True,
                    "effective_state": effective_state,
                }
                field_results.append(deterministic_result)
                continue
            field_results.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "status": "unresolved",
                    "value": None,
                    "conditions": [],
                    "exceptions": [],
                    "required_documents": [],
                    "evidence": [],
                    "confidence": 0.0,
                    "notes": "Focused-understanding call budget was exhausted.",
                    "audit": {"rounds": 0, "cache_hit": False},
                }
            )
            continue
        initial_candidate_count = len(evidence)
        result: dict[str, Any] | None = None
        last_normalized: dict[str, Any] | None = deterministic_result
        compact_projection_eligible = bool(
            amendment_deterministic_short_circuit
            and field.value_type in {"string", "string_array"}
            and len(effective_state.get("operations") or []) >= 3
        )
        prior_response: dict[str, Any] | None = (
            {
                "status": deterministic_result.get("status"),
                "value": deterministic_result.get("value"),
                "conditions": deterministic_result.get("conditions") or [],
                "exceptions": deterministic_result.get("exceptions") or [],
                "required_documents": deterministic_result.get("required_documents") or [],
                "evidence_source_ids": [
                    item.get("source_id")
                    for item in deterministic_result.get("evidence") or []
                    if item.get("source_id")
                ],
                "evidence_page_ids": [
                    item.get("page_id")
                    for item in deterministic_result.get("evidence") or []
                    if item.get("page_id")
                ],
                "confidence": deterministic_result.get("confidence"),
                "notes": deterministic_result.get("notes") or "",
            }
            if deterministic_result is not None
            else None
        )
        validation_errors: list[str] = list(deterministic_issues)
        field_faults: list[str] = []
        completeness_warnings: list[str] = list(deterministic_issues)
        blocking_grounding_issues = _blocking_grounding_issues(
            completeness_warnings
        )
        completeness_reviewed = deterministic_result is not None or bool(
            amendment_deterministic_short_circuit
            and package_field_state.get("status") == "resolved"
            and package_field_state.get("events")
            and (
                not _requires_exhaustive_coverage(field)
                or compact_projection_eligible
            )
        )
        verified = False
        media_parts: list[dict[str, Any]] = []
        pending_image_page_ids: list[str] = []
        requested_image_page_ids: list[str] = []
        sent_image_page_ids: list[str] = []
        automatic_image_page_ids: list[str] = []
        image_request_count = 0
        image_followup_calls = 0
        rounds_used = 0
        normalized_rounds: list[dict[str, Any]] = []
        nominal_rounds = max(1, max_rounds)
        seed = initial_responses.get(field.key) or {}
        seed_response = seed.get("response") if isinstance(seed, dict) else None
        seed_image_page_ids = [
            str(page_id)
            for page_id in (seed.get("image_page_ids") or [])
            if page_id
        ] if isinstance(seed, dict) else []
        if seed_image_page_ids:
            sent_image_page_ids.extend(seed_image_page_ids)
            automatic_image_page_ids.extend(seed_image_page_ids)

        if (
            seed_response is None
            and
            os.getenv("RFP_FIELD_EAGER_VISUAL_ENABLED", "0") == "1"
            and image_loader
            and max_images_per_field > 0
            and image_budget.available()
        ):
            initial_payload = _field_payload(
                schema,
                section,
                field,
                evidence,
                effective_state,
                1,
                prior_response,
                validation_errors,
            )
            initial_page_ids = initial_payload.get("visual_review_page_ids") or []
            if initial_payload.get("visual_required") and initial_page_ids:
                page_id = str(initial_page_ids[0])
                image = image_loader(page_id)
                if image and image.get("data") and image.get("mime_type"):
                    media_parts = [
                        {"text": f"PAGE_IMAGE page_id={page_id}"},
                        {
                            "inline_data": {
                                "mime_type": image["mime_type"],
                                "data": image["data"],
                            }
                        },
                    ]
                    pending_image_page_ids = [page_id]
                    automatic_image_page_ids = [page_id]

        for round_index in range(nominal_rounds + 1):
            seed_call = round_index == 0 and isinstance(seed_response, dict)
            provider_blocked_reason = provider_block.get()
            if provider_blocked_reason and not seed_call:
                break
            if round_index >= nominal_rounds and not media_parts and not seed_call:
                break
            automatic_image_call = bool(
                media_parts
                and set(pending_image_page_ids) & set(automatic_image_page_ids)
            )
            if seed_call:
                pass
            elif media_parts:
                if not image_budget.reserve():
                    break
                image_followup_calls += 1
                field_image_calls_made += 1
                sent_image_page_ids.extend(pending_image_page_ids)
            elif not standard_budget.reserve():
                break
            rounds_used += 1
            proof_repair = prior_response is not None and any(
                "required_documents" in error for error in validation_errors
            ) and not automatic_image_call
            if not seed_call and not media_parts:
                standard_calls_made += 1
            try:
                if seed_call:
                    raw = dict(seed_response)
                elif proof_repair:
                    repair = call(
                        system=(
                            "You are an evidence-grounded RFP proof-artifact resolver. "
                            "Repair only the required_documents lane and return concise JSON."
                        ),
                        payload=_proof_repair_payload(
                            schema,
                            section,
                            field,
                            evidence,
                            effective_state,
                            prior_response,
                            validation_errors,
                        ),
                        response_schema=_proof_repair_response_schema(),
                        media_parts=media_parts,
                        max_output_tokens=max(256, min(512, max_output_tokens)),
                        review=True,
                        allow_truncation_retry=False,
                    )
                    raw = (
                        repair
                        if isinstance(repair, dict) and repair.get("_truncated")
                        else _merge_proof_repair(
                            prior_response,
                            repair if isinstance(repair, dict) else {},
                        )
                    )
                elif media_parts and prior_response is not None:
                    raw = call(
                        system=(
                            "You verify one RFP field from a targeted page image and compact linked context. "
                            "Return the complete corrected answer as concise JSON only."
                        ),
                        payload=_targeted_visual_verification_payload(
                            schema,
                            section,
                            field,
                            evidence,
                            effective_state,
                            prior_response,
                            validation_errors,
                            pending_image_page_ids,
                        ),
                        response_schema=field_response_schema(field),
                        media_parts=media_parts,
                        max_output_tokens=max(128, min(900, max_output_tokens)),
                        review=True,
                        allow_truncation_retry=False,
                    )
                elif (
                    compact_projection_eligible
                    and prior_response is None
                ):
                    raw = call(
                        system=(
                            "You project validated RFP amendment events into one compact current value. "
                            "Return under 700 output tokens as concise JSON only and never restate source clauses."
                        ),
                        payload=_compact_projection_payload(
                            schema,
                            section,
                            field,
                            effective_state,
                            evidence,
                        ),
                        response_schema=_compact_projection_response_schema(field),
                        media_parts=[],
                        max_output_tokens=max(128, min(1200, max_output_tokens)),
                        review=True,
                        allow_truncation_retry=False,
                    )
                else:
                    raw = call(
                        system=(
                            "You are an evidence-grounded RFP field resolver. First obey the "
                            "application-compiled effective state, then resolve exactly one "
                            "client-configured field and return concise JSON only."
                        ),
                        payload=_field_payload(
                            schema,
                            section,
                            field,
                            evidence,
                            effective_state,
                            round_index + 1,
                            prior_response,
                            validation_errors,
                        ),
                        response_schema=field_response_schema(field),
                        media_parts=media_parts,
                        max_output_tokens=_field_output_budget(
                            field, effective_state, max_output_tokens
                        ),
                        review=round_index > 0,
                        allow_truncation_retry=False,
                    )
            except Exception as exc:
                failure = _compact_text(str(exc))[:500]
                field_faults.append(failure)
                validation_errors = [f"Model provider call failed: {failure}"]
                completeness_warnings = list(
                    dict.fromkeys([*completeness_warnings, *validation_errors])
                )
                hard_block = _hard_provider_block_reason(exc)
                provider_blocked_reason = (
                    provider_block.set(hard_block)
                    if hard_block
                    else provider_block.get()
                )
                if (
                    "maxOutputTokens" in failure
                    and last_normalized is None
                    and round_index + 1 < nominal_rounds
                ):
                    prior_response = None
                    continue
                break
            if isinstance(raw, dict) and raw.get("_truncated"):
                truncation_issue = (
                    "The latest model response was truncated; return a shorter answer "
                    "that preserves only material values, conditions, exceptions, proof "
                    "artifacts, and source IDs."
                )
                if last_normalized is not None:
                    result = last_normalized
                    completeness_warnings = list(
                        dict.fromkeys([*completeness_warnings, truncation_issue])
                    )
                    break
                if round_index + 1 < max(1, max_rounds):
                    validation_errors = [truncation_issue]
                    prior_response = None
                    continue
                raw = {
                    "status": "ambiguous",
                    "notes": "The field response remained truncated after compact retry.",
                }
            if not isinstance(raw, dict):
                raw = {"status": "ambiguous", "notes": "Model returned a non-object response."}
            if automatic_image_call and prior_response is not None:
                raw = _merge_visual_verification(prior_response, raw)
            if field.value_type == "money":
                not_applicable_ids = [
                    str(item.get("source_id") or "")
                    for item in evidence
                    if item.get("source_id")
                    and _explicit_not_applicable_value(
                        field,
                        [str(item["source_id"])],
                        retriever.units,
                    )
                ]
                if not_applicable_ids:
                    raw = {
                        **raw,
                        "status": "not_applicable",
                        "value": None,
                        "evidence_source_ids": not_applicable_ids[:4],
                    }
            prior_response = raw

            normalized, structural_errors = _normalize_result(
                raw,
                field,
                retriever.units,
                retriever.page_ids,
                {
                    str(item.get("source_id") or "")
                    for item in evidence
                    if item.get("source_id")
                },
            )
            normalized = _project_grounded_identifier(field, normalized, evidence)
            normalized = _project_unique_labeled_value(field, normalized, evidence)
            normalized = _project_controlling_scalar(
                field, normalized, effective_state
            )
            normalized = _project_deleted_members(normalized, effective_state)
            normalized = _project_template_conflict(
                normalized, evidence, effective_state
            )
            normalized = _project_explicit_formulas(
                field, normalized, evidence, effective_state
            )
            normalized = _project_prompt_exclusions(field, normalized)
            normalized = _project_currency_markers(normalized, evidence)
            normalized = _drop_serialized_collection_artifacts(normalized)
            normalized = _project_controlling_total_marks(
                normalized,
                evidence,
                effective_state,
            )
            normalized = _promote_explicit_exceptions(normalized)
            normalized = _prefer_more_complete_found(last_normalized, normalized)
            normalized_rounds.append(
                {
                    "round": rounds_used,
                    "raw": {
                        key: raw.get(key)
                        for key in (
                            "status", "value", "conditions", "exceptions",
                            "required_documents", "evidence_source_ids", "evidence_page_ids",
                        )
                    },
                    "normalized": {
                        key: normalized.get(key)
                        for key in (
                            "status", "value", "conditions", "exceptions",
                            "required_documents", "evidence",
                        )
                    },
                    "structural_errors": list(structural_errors),
                }
            )
            normalized_value = _phrase_text(str(normalized.get("value") or ""))
            if normalized.get("status") == "found" and normalized_value in {
                "not required",
                "no requirement",
                "not applicable",
                "none",
                "nil",
            }:
                controlling_ids = set(effective_state.get("controlling_source_ids") or [])
                controlling_text = _phrase_text(
                    " ".join(
                        str(item.get("text") or "")
                        for item in evidence
                        if item.get("source_id") in controlling_ids
                    )
                )
                grounded_zero_money = bool(
                    field.value_type == "money"
                    and controlling_ids
                    and _money_value_grounded(
                        normalized.get("value"),
                        [
                            source_id
                            for source_id in controlling_ids
                            if source_id in retriever.units
                        ],
                        retriever.units,
                        field,
                    )
                )
                if not (
                    _contains_phrase(controlling_text, normalized_value)
                    or grounded_zero_money
                ):
                    normalized["status"] = "not_found"
                    normalized["value"] = None
                    normalized["conditions"] = []
                    normalized["required_documents"] = []
                    normalized["exceptions"] = list(
                        dict.fromkeys(
                            [
                                *(normalized.get("exceptions") or []),
                                "No current requirement is stated in the controlling evidence.",
                            ]
                        )
                    )
            cited_ids = {
                str(item.get("source_id") or "")
                for item in normalized.get("evidence") or []
                if item.get("source_id")
            }
            unsupported_positive = (
                normalized.get("status") == "found"
                and field.validation.require_anchor_for_positive
                and field.retrieval_guidance.anchor_phrases
                and not (
                    field.value_type == "boolean"
                    and normalized.get("value") is False
                )
                and not any(
                    item.get("source_id") in cited_ids and item.get("anchor_match")
                    for item in evidence
                )
            )
            if unsupported_positive:
                normalized["status"] = "unresolved"
                normalized["value"] = None
                normalized["conditions"] = []
                normalized["required_documents"] = []
                normalized["exceptions"] = list(
                    dict.fromkeys(
                        [
                            *(normalized.get("exceptions") or []),
                            "The positive claim lacks configured anchor evidence.",
                        ]
                    )
                )
            if (
                not structural_errors
                and normalized.get("status") != "need_more_evidence"
            ):
                last_normalized = normalized
            validation_errors = list(structural_errors)
            request = (
                raw.get("evidence_request")
                if isinstance(raw.get("evidence_request"), dict)
                else {}
            )
            model_requested_image = (
                raw.get("status") == "need_more_evidence"
                and str(request.get("modality") or "")
                in {"page_image", "table_image"}
            )
            automatic_image_page_id = ""
            if (
                not media_parts
                and not model_requested_image
                and not automatic_image_page_ids
                and image_loader
                and max_images_per_field > 0
                and image_budget.available()
            ):
                automatic_image_page_id = _automatic_visual_page_id(
                    field,
                    evidence,
                    effective_state,
                    normalized,
                    validation_errors,
                )
            if raw.get("status") != "need_more_evidence" and not structural_errors:
                if round_index > 0:
                    completeness_reviewed = True
                completeness_warnings = _completeness_issues(
                    field,
                    normalized,
                    evidence,
                    effective_state,
                )
                blocking_grounding_issues = _blocking_grounding_issues(
                    completeness_warnings
                )
                if completeness_warnings and not automatic_image_page_id:
                    automatic_image_page_id = _automatic_visual_page_id(
                        field,
                        evidence,
                        effective_state,
                        normalized,
                        completeness_warnings,
                    )
                needs_negative_review = any(
                    warning.startswith(
                        "not_found conflicts with an explicit labeled value"
                    )
                    for warning in completeness_warnings
                )
                if completeness_warnings and not completeness_reviewed:
                    completeness_reviewed = True
                    audit_instruction = (
                        "Mandatory negative-evidence audit: verify the field is truly absent before returning not_found."
                        if needs_negative_review
                        else "Mandatory completeness audit: replace the prior response with a fully checked answer."
                    )
                    validation_errors = [audit_instruction] + completeness_warnings
                    if any(
                        "required_documents" in warning
                        for warning in completeness_warnings
                    ):
                        automatic_image_page_id = ""
                elif (
                    not blocking_grounding_issues
                    and (not completeness_warnings or completeness_reviewed)
                    and not automatic_image_page_id
                ):
                    result = normalized
                    verified = True
                    break
                else:
                    validation_errors = list(completeness_warnings)
                    if round_index + 1 >= nominal_rounds:
                        result = normalized
            if automatic_image_page_id:
                validation_errors = list(
                    dict.fromkeys(
                        [
                            "Mandatory one-page visual verification: inspect the supplied layout-sensitive page and replace the prior response with the complete current answer.",
                            *validation_errors,
                        ]
                    )
                )
            if round_index >= nominal_rounds:
                break

            modality, requested_sources, requested_pages, _adjacent, query = _request_details(
                raw,
                retriever,
                evidence,
                max(0, max_images_per_field),
            )
            explicit_image_request = model_requested_image and bool(requested_pages)
            automatic_image_request = bool(
                automatic_image_page_id and not explicit_image_request
            )
            if automatic_image_request:
                requested_pages = [automatic_image_page_id]
            should_send_image = explicit_image_request or automatic_image_request
            if explicit_image_request:
                image_request_count += 1
                requested_image_page_ids.extend(requested_pages)
            if round_index + 1 >= nominal_rounds and not should_send_image:
                break
            if (
                should_send_image
                and not image_budget.available()
            ):
                break
            if not should_send_image and not standard_budget.available():
                break
            query = query or _completeness_query(validation_errors, field)
            evidence = retriever.retrieve(
                schema,
                section,
                field,
                limit=max(initial_candidates, expanded_candidates),
                max_chars=max(initial_evidence_chars, expanded_evidence_chars),
                extra_query=query,
                requested_source_ids=[*amendment_source_ids, *requested_sources],
                requested_page_ids=requested_pages,
            )
            effective_state = retriever.compile_effective_state(
                evidence,
                field,
                package_field_state,
            )
            media_parts = []
            pending_image_page_ids = []
            if (
                should_send_image
                and image_loader
                and max_images_per_field > 0
                and image_budget.available()
            ):
                image_limit = 1 if automatic_image_request else max_images_per_field
                for page_id in requested_pages[:image_limit]:
                    image = image_loader(page_id)
                    if not image or not image.get("data") or not image.get("mime_type"):
                        continue
                    media_parts.extend(
                        [
                            {"text": f"PAGE_IMAGE page_id={page_id}"},
                            {
                                "inline_data": {
                                    "mime_type": image["mime_type"],
                                    "data": image["data"],
                                }
                            },
                        ]
                    )
                    pending_image_page_ids.append(page_id)
                    if automatic_image_request:
                        automatic_image_page_ids.append(page_id)
            if round_index + 1 >= nominal_rounds and not media_parts:
                break

        if result is None and last_normalized is not None:
            result = last_normalized
        if result is None:
            notes = "The field could not be resolved within its evidence and call budget."
            if validation_errors:
                notes = f"{notes} Validation: {'; '.join(validation_errors)[:700]}"
            result = {
                "key": field.key,
                "label": field.label,
                "status": "unresolved",
                "value": None,
                "conditions": [],
                "exceptions": [],
                "required_documents": [],
                "evidence": [],
                "confidence": 0.0,
                "notes": notes,
            }
        if result.get("status") == "not_found" and any(
            "not_found conflicts with an explicit labeled value" in warning
            for warning in completeness_warnings
        ):
            result["status"] = "unresolved"
            result["value"] = None
            result["notes"] = (
                "The model returned not_found despite an explicit labeled value in the "
                "current base RFP; no false absence was emitted."
            )
        result["audit"] = {
            "rounds": rounds_used,
            "cache_hit": False,
            "initial_candidate_count": initial_candidate_count,
            "final_candidate_count": len(evidence),
            "completeness_reviewed": completeness_reviewed,
            "verified": verified,
            "completeness_warnings": completeness_warnings,
            "blocking_grounding_issues": blocking_grounding_issues,
            "deterministic_projection": deterministic_result is not None,
            "image_requests": image_request_count,
            "image_requested_page_ids": list(
                dict.fromkeys(requested_image_page_ids)
            ),
            "image_sent_page_ids": list(dict.fromkeys(sent_image_page_ids)),
            "image_followup_calls": image_followup_calls,
            "automatic_image_verifications": len(
                set(automatic_image_page_ids)
            ),
            "automatic_image_page_ids": list(
                dict.fromkeys(automatic_image_page_ids)
            ),
            "normalized_rounds": normalized_rounds,
            "effective_state": effective_state,
            "fault_isolated": bool(field_faults),
            "faults": field_faults[:4],
        }
        if cache_path and result["status"] in _CACHEABLE_STATUSES and verified:
            _write_json_atomic(cache_path, result)
        field_results.append(result)
    return field_results[0], {
        "cache_hits": cache_hits,
        "standard_model_calls": standard_calls_made,
        "image_model_calls": field_image_calls_made,
    }
