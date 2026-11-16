"""Shared call state and failure contracts for field understanding."""

from __future__ import annotations

import threading
from typing import Any

from .field_core import (
    AMENDMENT_COMPILER_VERSION,
    COMPILER_VERSION,
    OUTPUT_SCHEMA_VERSION,
    _compact_text,
    _source_digest,
)
from .schema import (
    RfpFieldSpec,
    RfpUnderstandingSchema,
    schema_field_count,
)

class _SharedCallBudget:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.used = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        with self._lock:
            return self.used < self.limit

    def reserve(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


class _SharedProviderBlock:
    def __init__(self, reason: str = "") -> None:
        self._reason = reason
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            return self._reason

    def set(self, reason: str) -> str:
        if not reason:
            return self.get()
        with self._lock:
            if not self._reason:
                self._reason = reason
            return self._reason


def _unresolved_field_result(
    field: RfpFieldSpec,
    reason: str,
    *,
    error_code: str = "field_processing_error",
) -> dict[str, Any]:
    """Return the existing field contract for an isolated processing failure."""
    return {
        "key": field.key,
        "label": field.label,
        "status": "unresolved",
        "value": None,
        "conditions": [],
        "exceptions": [],
        "required_documents": [],
        "evidence": [],
        "confidence": 0.0,
        "notes": f"This field needs human review: {_compact_text(reason)[:700]}",
        "audit": {
            "rounds": 0,
            "cache_hit": False,
            "verified": False,
            "fault_isolated": True,
            "error_code": error_code,
        },
    }


def _failed_understanding_result(
    schema: RfpUnderstandingSchema,
    source_units: dict[str, dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    fields = [
        _unresolved_field_result(
            field,
            reason,
            error_code="field_compilation_boundary_error",
        )
        for section in schema.sections
        for field in section.fields
    ]
    sections: list[dict[str, Any]] = []
    cursor = 0
    for section in schema.sections:
        section_fields = fields[cursor : cursor + len(section.fields)]
        cursor += len(section.fields)
        sections.append(
            {"key": section.key, "label": section.label, "fields": section_fields}
        )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "schema_id": schema.schema_id,
        "source_digest": _source_digest(source_units),
        "status": "failed",
        "amendment_state": {"status": "not_run", "fields": {}},
        "sections": sections,
        "audit": {
            "compiler_version": COMPILER_VERSION,
            "source_units": len(source_units),
            "fields_total": len(fields),
            "fields_found": 0,
            "fields_unresolved": len(fields),
            "fields_unverified": len(fields),
            "model_calls": 0,
            "fault_isolated": True,
            "error_code": "field_compilation_boundary_error",
            "error_detail": _compact_text(reason)[:700],
        },
    }
