"""Shared constants and JSON/model conversion helpers for BHEL evaluation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from collections.abc import Mapping


RESULT_STATUSES = (
    "met",
    "not_met",
    "insufficient_evidence",
    "needs_review",
    "not_applicable",
    "not_evaluated",
)
_DISABLED = {"", "0", "false", "no", "off", "disabled", "none"}
_PROFILE_NAMES = {
    "bhel",
    "bhel.v1",
    "bhel-purchase-materials",
    "bhel_purchase_materials",
    "enabled",
    "true",
    "yes",
    "1",
}
_PROFILE_FILE = "bid_evaluation.bhel.v1.json"
_BLOCKING = {"mandatory_blocking", "conditional_blocking", "internal_gate"}
_SOURCE_FIELDS = (
    "text",
    "content",
    "ocr_text",
    "native_text",
    "page_text",
    "markdown",
    "source_text",
)
_EVALUATION_SCOPES = {
    "technical_qualification",
    "internal_control",
    "commercial_out_of_scope",
    "post_award",
}
_BASELINE_SEMANTIC_KEYS = {
    "emd_status": "bhel.emd",
    "mse_status": "bhel.mse",
    "make_in_india": "bhel.make_in_india",
    "integrity_pact": "bhel.integrity_pact",
    "deviation": "bhel.deviation",
    "pqr_qualification": "bhel.pqr",
    "third_party_inspection": "bhel.third_party_inspection",
}
_SOURCE_WRAPPER_KEYS = {
    "source_units",
    "sourceunits",
    "units",
    "pages",
    "documents",
    "items",
}
_SOURCE_REFERENCE_KEYS = {
    "source_references",
    "source_refs",
    "citations",
    "references",
    "rfp_source_refs",
    "evidence",
    "supporting_evidence",
}
_UNDERSTANDING_CONTAINERS = {
    "field_understanding",
    "sections",
    "fields",
    "field_entries",
    "frozen_contract",
    "criteria",
    "requirements",
}
_FINDING_LIST_KEYS = (
    "findings",
    "criteria_results",
    "checks",
    "results",
    "items",
)
_ID_FIELDS = ("check_id", "criterion_id", "id", "key", "field", "name", "title", "label")


def _model_data(value: Any) -> Any:
    """Return mappings/lists for both plain JSON and Pydantic inputs."""
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            return _model_data(dumper(mode="python"))
        except TypeError:
            return _model_data(dumper())
    legacy_dumper = getattr(value, "dict", None)
    if callable(legacy_dumper) and not isinstance(value, (Mapping, list, tuple, set)):
        return _model_data(legacy_dumper())
    if isinstance(value, Mapping):
        return {key: _model_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_model_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_model_data(item) for item in value)
    return value


def _mapping(value: Any) -> dict[str, Any]:
    value = _model_data(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []
