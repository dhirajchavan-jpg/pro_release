"""Explicit compatibility facade for field-understanding compilation."""

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
from .field_understanding_state import (
    _SharedCallBudget,
    _SharedProviderBlock,
    _failed_understanding_result,
    _unresolved_field_result,
)
from .field_understanding_batch import (
    _batch_field_response_schema,
    _initial_field_batch_responses,
)
from .field_understanding_compiler import (
    _compile_field_understanding_impl,
    compile_field_understanding,
)

if __name__ == "__main__":
    sample_schema = RfpUnderstandingSchema.model_validate(
        {
            "schema_id": "self-check",
            "sections": [
                {
                    "key": "bidding",
                    "label": "Bidding",
                    "fields": [
                        {
                            "key": "allowed_legal_entities",
                            "label": "Allowed legal entities",
                            "description": "Legal forms permitted to bid.",
                            "retrieval_guidance": {
                                "aliases": ["company", "partnership", "joint venture"]
                            },
                        }
                    ],
                }
            ],
        }
    )
    sample_units = {
        "u1": {
            "text": "The bidder shall be a company registered under the Companies Act.",
            "page": 3,
            "page_id": "p3",
            "document": "d1",
            "section_path": ["Eligibility"],
        }
    }
    hits = LocalFieldRetriever(sample_units).retrieve(
        sample_schema,
        sample_schema.sections[0],
        sample_schema.sections[0].fields[0],
        limit=3,
        max_chars=2000,
    )
    assert hits and hits[0]["source_id"] == "u1"

__all__ = [
    "annotations",
    "ThreadPoolExecutor",
    "as_completed",
    "json",
    "os",
    "Path",
    "threading",
    "Any",
    "Callable",
    "AMENDMENT_COMPILER_VERSION",
    "COMPILER_VERSION",
    "OUTPUT_SCHEMA_VERSION",
    "_CACHEABLE_STATUSES",
    "_amendment_state_source_ids",
    "_automatic_visual_page_id",
    "_compact_projection_response_schema",
    "_compact_text",
    "_contains_phrase",
    "_field_cache_path",
    "_field_output_budget",
    "_hard_provider_block_reason",
    "_is_placeholder_value",
    "_phrase_text",
    "_prefer_more_complete_found",
    "_project_grounded_identifier",
    "_project_unique_labeled_value",
    "_proof_repair_response_schema",
    "_requires_exhaustive_coverage",
    "_source_digest",
    "_write_json_atomic",
    "field_response_schema",
    "LocalFieldRetriever",
    "_reduce_field_amendments",
    "compile_package_amendments",
    "_compact_projection_payload",
    "_field_payload",
    "_merge_proof_repair",
    "_merge_visual_verification",
    "_proof_repair_payload",
    "_targeted_visual_verification_payload",
    "_blocking_grounding_issues",
    "_completeness_issues",
    "_completeness_query",
    "_deterministic_scalar_result",
    "_drop_serialized_collection_artifacts",
    "_explicit_not_applicable_value",
    "_money_value_grounded",
    "_normalize_result",
    "_project_controlling_scalar",
    "_project_controlling_total_marks",
    "_project_currency_markers",
    "_project_deleted_members",
    "_project_explicit_formulas",
    "_project_prompt_exclusions",
    "_project_template_conflict",
    "_promote_explicit_exceptions",
    "_request_details",
    "RfpFieldSpec",
    "RfpSectionSpec",
    "RfpUnderstandingSchema",
    "schema_field_count",
    "_SharedCallBudget",
    "_SharedProviderBlock",
    "_unresolved_field_result",
    "_failed_understanding_result",
    "_batch_field_response_schema",
    "_initial_field_batch_responses",
    "compile_field_understanding",
    "_compile_field_understanding_impl",
]
