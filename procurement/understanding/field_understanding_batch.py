"""Batch field-resolution helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable

from .field_core import (
    _CACHEABLE_STATUSES,
    _amendment_state_source_ids,
    _field_cache_path,
    _hard_provider_block_reason,
    _is_placeholder_value,
    _source_digest,
    field_response_schema,
)
from .field_prompts import _field_payload
from .field_results import _completeness_issues, _deterministic_scalar_result
from .field_retrieval import LocalFieldRetriever
from .schema import RfpFieldSpec, RfpSectionSpec, RfpUnderstandingSchema
from .field_understanding_state import _SharedCallBudget, _SharedProviderBlock

def _batch_field_response_schema(fields: list[RfpFieldSpec]) -> dict[str, Any]:
    item = json.loads(json.dumps(field_response_schema(fields[0])))
    item["properties"] = {
        "key": {"type": "string", "enum": [field.key for field in fields]},
        **item["properties"],
    }
    item["required"] = ["key", *item["required"]]
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "minItems": len(fields),
                "maxItems": len(fields),
                "items": item,
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _initial_field_batch_responses(
    schema: RfpUnderstandingSchema,
    retriever: LocalFieldRetriever,
    work_items: list[tuple[int, RfpSectionSpec, RfpFieldSpec]],
    amendment_state: dict[str, Any],
    *,
    call: Callable[..., dict[str, Any]],
    checkpoint_dir: Path | None,
    image_loader: Callable[[str], dict[str, str] | None] | None,
    model_tag: str,
    initial_candidates: int,
    initial_evidence_chars: int,
    max_images_per_field: int,
    max_output_tokens: int,
    field_concurrency: int,
    standard_budget: _SharedCallBudget,
    image_budget: _SharedCallBudget,
    provider_block: _SharedProviderBlock,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    batch_size = max(1, int(os.getenv("RFP_FIELD_BATCH_SIZE", "1")))
    if batch_size <= 1:
        return {}, {"model_calls": 0, "standard_model_calls": 0, "image_model_calls": 0}

    grouped: dict[str, list[tuple[RfpSectionSpec, RfpFieldSpec, dict[str, Any], list[str]]]] = {}
    eager_visual = os.getenv("RFP_FIELD_EAGER_VISUAL_ENABLED", "0") == "1"
    digest = _source_digest(retriever.units)
    for _index, section, field in work_items:
        package_field_state = dict(amendment_state.get("fields", {}).get(field.key) or {})
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
                    continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        amendment_source_ids = _amendment_state_source_ids(package_field_state)
        evidence = retriever.retrieve(
            schema,
            section,
            field,
            limit=max(1, initial_candidates),
            max_chars=max(1000, initial_evidence_chars),
            requested_source_ids=amendment_source_ids,
        )
        if not evidence:
            continue
        effective_state = retriever.compile_effective_state(
            evidence,
            field,
            package_field_state,
        )
        deterministic = _deterministic_scalar_result(field, effective_state, retriever.units)
        if deterministic is not None and not _completeness_issues(
            field, deterministic, evidence, effective_state
        ):
            continue
        payload = _field_payload(
            schema,
            section,
            field,
            evidence,
            effective_state,
            1,
            deterministic,
            _completeness_issues(field, deterministic, evidence, effective_state)
            if deterministic is not None
            else [],
        )
        image_page_ids = (
            [str(payload["visual_review_page_ids"][0])]
            if eager_visual
            and image_loader
            and max_images_per_field > 0
            and payload.get("visual_required")
            and payload.get("visual_review_page_ids")
            else []
        )
        dossier = {
            key: value
            for key, value in payload.items()
            if key not in {"task", "schema_id", "schema_prompt", "rules"}
        }
        signature = json.dumps(field_response_schema(field), sort_keys=True)
        grouped.setdefault(signature, []).append((section, field, dossier, image_page_ids))

    plans = [
        group[offset : offset + batch_size]
        for group in grouped.values()
        for offset in range(0, len(group), batch_size)
    ]
    responses: dict[str, dict[str, Any]] = {}
    audit = {"model_calls": 0, "standard_model_calls": 0, "image_model_calls": 0}
    audit_lock = threading.Lock()

    def run_batch(
        batch: list[tuple[RfpSectionSpec, RfpFieldSpec, dict[str, Any], list[str]]],
    ) -> dict[str, dict[str, Any]]:
        if provider_block.get():
            return {}
        media_parts: list[dict[str, Any]] = []
        image_pages_by_field: dict[str, list[str]] = {}
        loaded_pages: set[str] = set()
        for _section, field, _dossier, page_ids in batch:
            for page_id in page_ids:
                image_pages_by_field[field.key] = [page_id]
                if page_id in loaded_pages or not image_loader:
                    continue
                image = image_loader(page_id)
                if not image or not image.get("data") or not image.get("mime_type"):
                    continue
                loaded_pages.add(page_id)
                relevant = [
                    item_field.key
                    for _item_section, item_field, _item_dossier, item_pages in batch
                    if page_id in item_pages
                ]
                media_parts.extend(
                    [
                        {
                            "text": (
                                f"PAGE_IMAGE page_id={page_id} relevant_fields="
                                + ",".join(relevant)
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": image["mime_type"],
                                "data": image["data"],
                            }
                        },
                    ]
                )
        budget = image_budget if media_parts else standard_budget
        if not budget.reserve():
            return {}
        with audit_lock:
            audit["model_calls"] += 1
            audit["image_model_calls" if media_parts else "standard_model_calls"] += 1
        fields = [field for _section, field, _dossier, _pages in batch]
        try:
            raw = call(
                system=(
                    "Resolve each independent RFP field from only its supplied dossier. "
                    "Return exactly one concise result per field key as JSON."
                ),
                payload={
                    "task": "resolve_rfp_field_batch",
                    "schema_id": schema.schema_id,
                    "rules": [
                        "Treat every dossier independently and return every listed field key exactly once.",
                        "Use only source IDs, page IDs, text, effective state, and labelled page images supplied for that field.",
                        "Preserve all material conditions, exceptions, thresholds, and named bidder proof.",
                        "Use need_more_evidence rather than guessing when a dossier is insufficient.",
                        "Every found result must cite supplied source IDs or page IDs.",
                    ],
                    "field_dossiers": [dossier for _section, _field, dossier, _pages in batch],
                },
                response_schema=_batch_field_response_schema(fields),
                media_parts=media_parts,
                max_output_tokens=max(
                    512,
                    min(max_output_tokens, max(1200, 1200 * len(fields))),
                ),
                review=False,
                allow_truncation_retry=False,
            )
        except Exception as exc:
            hard_block = _hard_provider_block_reason(exc)
            if hard_block:
                provider_block.set(hard_block)
            return {}
        results = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(results, list):
            return {}
        allowed = {field.key for field in fields}
        collected: dict[str, dict[str, Any]] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if key not in allowed or key in collected:
                continue
            response = dict(item)
            response.pop("key", None)
            collected[key] = {
                "response": response,
                "image_page_ids": image_pages_by_field.get(key, []),
            }
        return collected

    workers = min(max(1, field_concurrency), len(plans)) if plans else 0
    if workers == 1:
        batch_results = [run_batch(plan) for plan in plans]
    elif workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            batch_results = [future.result() for future in as_completed(
                [executor.submit(run_batch, plan) for plan in plans]
            )]
    else:
        batch_results = []
    for result in batch_results:
        responses.update(result)
    return responses, audit
