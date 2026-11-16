"""Field response schemas, deterministic projections, and cache helpers."""

from __future__ import annotations

from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from procurement.platform.files import write_json_atomic

from .field_binding import _direct_scalar_update_value, _requires_exhaustive_coverage
from .field_units import _table_cells, _unit_text
from .lexical import _compact_text, _contains_phrase, _phrase_text, _tokens
from .schema import RfpFieldSpec, RfpSectionSpec, RfpUnderstandingSchema


COMPILER_VERSION = "rfp-focused-fields-v78-basic-metadata-resilience"

AMENDMENT_COMPILER_VERSION = "rfp-package-amendments-v52-amendment-coverage"

OUTPUT_SCHEMA_VERSION = "rfp-field-understanding.v1"

_CACHEABLE_STATUSES = {"found", "not_found", "not_applicable"}

_MAX_RESULT_ITEMS = 24

_PLACEHOLDER_VALUES = {
    "buyer",
    "buyer organization",
    "buyer organisation",
    "name of buyer",
    "name of client",
    "name of employer",
    "name of organisation",
    "name of organization",
    "name of purchaser",
    "name of supplier",
    "principal",
    "purchaser",
    "to be filled",
    "to be specified",
}

_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)

_GEM_BID_RE = re.compile(r"(?<![A-Za-z0-9])GEM\s*/\s*\d{4}\s*/\s*B\s*/\s*\d+(?![A-Za-z0-9])", re.IGNORECASE)


def _hard_provider_block_reason(exc: Exception) -> str:
    failure = _compact_text(str(exc))[:500]
    lowered = failure.casefold()
    category = str(getattr(exc, "category", "") or "")
    if bool(getattr(exc, "retryable", False)) or category in {
        "configuration",
        "http_401",
        "http_403",
        "http_404",
        "provider_error",
    }:
        return failure
    return failure if any(
        cue in lowered
        for cue in (
            "resource_exhausted",
            "resource exhausted",
            "spending cap",
            "spend cap",
            "quota exceeded",
            "monthly spending",
            "http error 429",
            "request failed 429",
        )
    ) else ""


_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)
_GEM_BID_RE = re.compile(r"(?<![A-Za-z0-9])GEM\s*/\s*\d{4}\s*/\s*B\s*/\s*\d+(?![A-Za-z0-9])", re.IGNORECASE)


def _is_identifier_field(field: RfpFieldSpec) -> bool:
    if field.value_type != "string":
        return False
    descriptor = _phrase_text(f"{field.key} {field.label} {field.description}")
    return field.key.endswith("_id") or any(
        cue in descriptor
        for cue in (
            "identifier",
            "identification number",
            "reference number",
            "tender number",
            "bid number",
            "enquiry number",
        )
    )


def _identifier_candidates(text: str) -> list[str]:
    return [
        candidate
        for candidate in _IDENTIFIER_TOKEN_RE.findall(text)
        if len(candidate) >= 6
        and any(character.isalpha() for character in candidate)
        and any(character.isdigit() for character in candidate)
    ]


def _project_grounded_identifier(
    field: RfpFieldSpec,
    result: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if field.key == "tender_id":
        gem_evidence = [
            (re.sub(r"\s+", "", match.group(0)).upper(), item)
            for item in evidence or []
            for match in _GEM_BID_RE.finditer(str(item.get("text") or item.get("quote") or ""))
            if any(
                label in _phrase_text(str(item.get("text") or item.get("quote") or ""))
                for label in ("bid number", "bid no", "gem tender")
            )
        ]
        gem_numbers = {number for number, _item in gem_evidence}
        if len(gem_numbers) == 1:
            number = gem_numbers.pop()
            item = next(item for candidate, item in gem_evidence if candidate == number)
            quote = _compact_text(item.get("text") or item.get("quote"))[:300]
            result.update(
                status="found",
                value=number,
                evidence=[
                    {
                        "source_id": str(item.get("source_id") or ""),
                        "page_id": str(item.get("page_id") or ""),
                        "quote": quote,
                    }
                ],
                confidence=max(float(result.get("confidence") or 0), 0.99),
            )
            return result
    if (
        result.get("status") != "found"
        or not _is_identifier_field(field)
        or not isinstance(result.get("value"), str)
    ):
        return result
    value = _compact_text(result["value"])
    normalized_value = re.sub(r"[^a-z0-9]", "", value.casefold())
    candidates = [
        candidate
        for item in result.get("evidence") or []
        for candidate in _identifier_candidates(str(item.get("quote") or ""))
    ]
    if not normalized_value or not candidates:
        return result
    grouped: dict[str, list[str]] = {}
    for candidate in candidates:
        normalized = re.sub(r"[^a-z0-9]", "", candidate.casefold())
        grouped.setdefault(normalized, []).append(candidate)
    if normalized_value in grouped:
        result["value"] = grouped[normalized_value][0]
        return result
    ranked = sorted(
        (
            SequenceMatcher(None, normalized_value, normalized).ratio(),
            len(values),
            normalized,
            values[0],
        )
        for normalized, values in grouped.items()
    )
    if not ranked or ranked[-1][0] < 0.9:
        return result
    best = ranked[-1]
    tied = [item for item in ranked if item[:2] == best[:2]]
    if len({item[2] for item in tied}) == 1:
        result["value"] = best[3]
    return result


def _project_unique_labeled_value(
    field: RfpFieldSpec,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    if result.get("status") == "found":
        return result
    candidates: list[tuple[Any, dict[str, Any]]] = []
    if field.value_type == "string":
        phrases = [field.label, *field.retrieval_guidance.aliases]
        for item in evidence:
            cells = _table_cells(item)
            for index, cell in enumerate(cells[:-1]):
                if not any(_contains_phrase(_phrase_text(cell), phrase) for phrase in phrases):
                    continue
                value = _compact_text(cells[index + 1])
                if value and not _is_placeholder_value(value) and len(value) <= 300:
                    candidates.append((value, item))
    elif field.value_type in {"date", "money", "integer", "number"}:
        phrases = [field.label, *field.retrieval_guidance.aliases]
        for item in evidence:
            cells = _table_cells(item)
            for index, cell in enumerate(cells[:-1]):
                if not any(_contains_phrase(_phrase_text(cell), phrase) for phrase in phrases):
                    continue
                probe = {**item, "text": f"{cell} | {cells[index + 1]}"}
                value = _direct_scalar_update_value(field, probe)
                if value:
                    candidates.append((value, item))
    grouped = {
        _phrase_text(str(value)): (value, item)
        for value, item in candidates
        if _phrase_text(str(value))
    }
    if len(grouped) != 1:
        return result
    value, item = next(iter(grouped.values()))
    if field.value_type == "integer":
        value = int(float(value))
    elif field.value_type == "number":
        value = float(value)
        if value.is_integer():
            value = int(value)
    result.update(
        status="found",
        value=value,
        evidence=[
            {
                "source_id": str(item.get("source_id") or ""),
                "page_id": str(item.get("page_id") or ""),
                "quote": _compact_text(item.get("text"))[:300],
            }
        ],
        confidence=max(float(result.get("confidence") or 0), 0.99),
    )
    return result


def _prefer_more_complete_found(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if not previous or previous.get("status") != "found" or current.get("status") != "found":
        return current
    old_value, new_value = previous.get("value"), current.get("value")
    if isinstance(old_value, str) and isinstance(new_value, str):
        old_tokens = {token.rstrip("s") for token in _tokens(old_value)}
        new_tokens = {token.rstrip("s") for token in _tokens(new_value)}
        if new_tokens and new_tokens <= old_tokens and len(old_value) > len(new_value):
            return previous
    if isinstance(old_value, list) and isinstance(new_value, list):
        old_items = {_phrase_text(str(item)) for item in old_value}
        new_items = {_phrase_text(str(item)) for item in new_value}
        if new_items and new_items < old_items:
            return previous
    return current


def _has_ocr_near_token(token: str, candidates: set[str]) -> bool:
    return any(
        abs(len(token) - len(candidate)) <= 2
        and SequenceMatcher(None, token, candidate).ratio() >= 0.8
        for candidate in candidates
    )


def _is_placeholder_value(value: Any) -> bool:
    return isinstance(value, str) and _phrase_text(value) in _PLACEHOLDER_VALUES


def _value_schema(value_type: str) -> dict[str, Any]:
    if value_type == "string":
        return {
            "type": "string",
            "maxLength": 1200,
            "description": "Concise direct value only; no evidence narration, conditions, or line breaks.",
        }
    if value_type == "date":
        return {
            "type": "string",
            "description": "Date value only; no explanation, evidence narration, or line breaks.",
        }
    if value_type == "money":
        return {
            "type": "string",
            "description": "Monetary amount and currency only; put exemptions and terms in conditions.",
        }
    if value_type == "string_array":
        return {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_RESULT_ITEMS,
            "items": {
                "type": "string",
                "maxLength": 500,
                "description": "One concise value without evidence narration or line breaks.",
            },
        }
    if value_type == "number":
        return {"type": "number"}
    if value_type == "integer":
        return {"type": "integer"}
    if value_type == "boolean":
        return {"type": "boolean"}
    if value_type == "object_array":
        return {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_RESULT_ITEMS,
            "items": {"type": "object"},
        }
    return {"type": "object"}


def field_response_schema(field: RfpFieldSpec) -> dict[str, Any]:
    exhaustive = _requires_exhaustive_coverage(field)
    item_limit = (
        8
        if field.value_type == "string" and exhaustive
        else 4
        if field.value_type == "string"
        else 12
        if field.value_type == "string_array"
        else 8
    )
    value_schema = _value_schema(field.value_type)
    if field.value_type == "string" and exhaustive:
        value_schema["maxLength"] = 1800
    if value_schema.get("type") == "array":
        value_schema["maxItems"] = item_limit
    request = {
        "type": "object",
        "properties": {
            "modality": {
                "type": "string",
                "enum": ["more_text", "page_image", "table_image"],
            },
            "query": {"type": "string", "description": "Retrieval query of at most 12 words."},
            "source_ids": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
            "page_ids": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
            "include_adjacent_pages": {"type": "integer", "minimum": 0, "maximum": 2},
        },
        "required": ["modality", "query"],
    }
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "found",
                    "not_found",
                    "not_applicable",
                    "ambiguous",
                    "need_more_evidence",
                ],
            },
            "value": value_schema,
            "conditions": {
                "type": "array",
                "maxItems": item_limit,
                "description": "Every material threshold, scope limit, period, modality, dependency, and mandatory condition for this field.",
                "items": {"type": "string", "maxLength": 300},
            },
            "exceptions": {
                "type": "array",
                "maxItems": item_limit,
                "description": "Every exemption, relaxation, alternative, carve-out, fallback, deletion, or unresolved source conflict for this field.",
                "items": {"type": "string", "maxLength": 300},
            },
            "required_documents": {
                "type": "array",
                "maxItems": item_limit,
                "description": "Every named bid-stage proof or scored evaluation artifact supporting this field, including formats, certificates, declarations, undertakings, letters, purchase orders, audited statements, presentations, walkthroughs, case studies, demos, logs, data samples, and test summaries.",
                "items": {"type": "string", "maxLength": 200},
            },
            "evidence_source_ids": {
                "type": "array",
                "maxItems": min(12, item_limit),
                "description": "Exact source_id values copied from supplied evidence; empty when no evidence applies.",
                "items": {"type": "string"},
            },
            "evidence_page_ids": {
                "type": "array",
                "maxItems": min(12, item_limit),
                "description": "Exact page_id values copied from supplied evidence; empty when no evidence applies.",
                "items": {"type": "string"},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {
                "type": "string",
                "maxLength": 160,
                "description": "At most one concise sentence.",
            },
            "evidence_request": request,
        },
        "required": ["status", "evidence_source_ids", "evidence_page_ids", "confidence"],
    }


def _compact_projection_response_schema(field: RfpFieldSpec) -> dict[str, Any]:
    item_limit = 12 if field.value_type == "string_array" else 4
    value_schema = _value_schema(field.value_type)
    if value_schema.get("type") == "string":
        value_schema["maxLength"] = 1200
    elif value_schema.get("type") == "array":
        value_schema["maxItems"] = item_limit
        value_schema["items"] = {"type": "string", "maxLength": 300}
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["found", "ambiguous"]},
            "value": value_schema,
            "conditions": {
                "type": "array",
                "maxItems": item_limit,
                "items": {"type": "string", "maxLength": 300},
            },
            "exceptions": {
                "type": "array",
                "maxItems": item_limit,
                "items": {"type": "string", "maxLength": 300},
            },
            "required_documents": {
                "type": "array",
                "maxItems": item_limit,
                "items": {"type": "string", "maxLength": 200},
            },
            "evidence_source_ids": {
                "type": "array",
                "maxItems": min(12, item_limit),
                "items": {"type": "string"},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "string", "maxLength": 160},
        },
        "required": [
            "status",
            "value",
            "conditions",
            "exceptions",
            "required_documents",
            "evidence_source_ids",
            "confidence",
            "notes",
        ],
    }


def _proof_repair_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "required_documents": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string"},
            },
            "evidence_source_ids": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string"},
            },
            "evidence_page_ids": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "required_documents",
            "evidence_source_ids",
            "evidence_page_ids",
            "confidence",
        ],
    }


def _source_digest(source_units: dict[str, dict[str, Any]]) -> str:
    digest = sha256()
    for source_id, unit in sorted(source_units.items()):
        digest.update(source_id.encode("utf-8", errors="ignore"))
        digest.update(_unit_text(unit).encode("utf-8", errors="ignore"))
        digest.update(
            json.dumps(
                {
                    "source_role": unit.get("source_role"),
                    "source_file": unit.get("source_file"),
                    "unit_type": unit.get("unit_type"),
                    "clause_number": unit.get("clause_number"),
                    "section_path": unit.get("section_path"),
                    "table_headers": unit.get("table_headers"),
                    "revision_sequence": unit.get("revision_sequence"),
                    "document_order": unit.get("document_order"),
                    "authority_rank": unit.get("authority_rank"),
                    "effective_state": unit.get("effective_state"),
                    "superseded_by_document_id": unit.get("superseded_by_document_id"),
                    "superseded_by_scope": unit.get("superseded_by_scope"),
                    "section_scope": unit.get("section_scope"),
                    "replacement_scope": unit.get("replacement_scope"),
                },
                ensure_ascii=True,
                sort_keys=True,
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _field_cache_path(
    checkpoint_dir: Path,
    source_digest: str,
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    model_tag: str,
    amendment_state: dict[str, Any] | None = None,
) -> Path:
    fingerprint = sha256(
        json.dumps(
            {
                "compiler": COMPILER_VERSION,
                "source": source_digest,
                "schema_id": schema.schema_id,
                "schema_prompt": schema.prompt,
                "section": {
                    "key": section.key,
                    "label": section.label,
                    "prompt": section.prompt,
                },
                "field": field.model_dump(mode="json"),
                "model": model_tag,
                "amendment_state": amendment_state or {},
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    safe_key = re.sub(r"[^a-z0-9_-]+", "-", field.key.casefold()).strip("-")
    return checkpoint_dir / f"{safe_key[:32]}-{fingerprint[:20]}.json"


def _write_json_atomic(path: Path, value: Any) -> None:
    write_json_atomic(path, value, default=str)
