"""Source-unit normalization and provenance matching for BHEL evaluation."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Iterable
from collections.abc import Mapping

from .bhel_evaluation_common import (
    _SOURCE_FIELDS,
    _SOURCE_WRAPPER_KEYS,
    _model_data,
    _text,
)


def _iter_strings(value: Any, seen: set[int] | None = None) -> Iterable[str]:
    seen = seen or set()
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (Mapping, list, tuple, set)):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item, seen)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_strings(item, seen)


def _source_reference(
    value: Mapping[str, Any],
    source_type: str,
    *,
    inherited_source_id: str | None = None,
    quote: str | None = None,
) -> dict[str, Any] | None:
    """Create one canonical, locatable source reference or reject the value."""
    source_id = str(inherited_source_id or "").strip()
    if not source_id:
        for key in ("source_id", "sourceId", "unit_id", "unitId", "page_id", "pageId"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                source_id = str(candidate).strip()
                break
    document_id = value.get("document_id") or value.get("documentId") or value.get("document")
    page_id = value.get("page_id") or value.get("pageId")
    page = value.get("page") or value.get("page_number") or value.get("pageNumber")
    if not source_id:
        return None

    reference: dict[str, Any] = {"source_id": source_id, "source_type": source_type}
    for canonical, keys in (
        ("document_id", ("document_id", "documentId", "document")),
        ("page_id", ("page_id", "pageId")),
        ("page", ("page", "page_number", "pageNumber")),
        ("file_name", ("file_name", "filename", "fileName")),
        ("source_file", ("source_file", "sourceFile")),
    ):
        for key in keys:
            if value.get(key) not in (None, ""):
                reference[canonical] = value[key]
                break
    if quote not in (None, ""):
        reference["quote"] = str(quote).strip()
    return reference


def _source_records(value: Any, source_type: str) -> list[dict[str, Any]]:
    """Read only real source units; never flatten an understanding payload."""
    value = _model_data(value)
    records: list[dict[str, Any]] = []

    # The understanding result is metadata and a set of field decisions. Its
    # text is not a tender citation. Source units are the authoritative corpus.
    # An embedded source_units map is accepted only as a source-unit corpus.
    if source_type == "tender_understanding":
        if isinstance(value, Mapping):
            for key in ("source_units", "sourceUnits"):
                if key in value:
                    return _source_records(value[key], "source_unit")
        return []

    def visit(item: Any, inherited_source_id: str | None = None) -> None:
        if isinstance(item, Mapping):
            text_parts = [
                _text(item.get(key)).strip()
                for key in _SOURCE_FIELDS
                if _text(item.get(key)).strip()
            ]
            quote = _text(item.get("quote")).strip()
            if text_parts or quote:
                reference = _source_reference(
                    item,
                    source_type,
                    inherited_source_id=inherited_source_id,
                    quote=quote or None,
                )
                if reference:
                    records.append({"text": " ".join(text_parts) or quote, "reference": reference})
                return
            for key, child in item.items():
                key_name = str(key).casefold()
                if key_name in _SOURCE_WRAPPER_KEYS:
                    visit(child)
                elif inherited_source_id is None and isinstance(child, Mapping):
                    # A source_units mapping is keyed by the stable source ID.
                    visit(child, str(key))
                else:
                    visit(child, inherited_source_id)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child, inherited_source_id)

    visit(value)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        marker = json.dumps(
            {"text": record["text"], "reference": record["reference"]},
            sort_keys=True,
            default=str,
        )
        if marker not in seen:
            seen.add(marker)
            unique.append(record)
    return unique


def _normalize_source_references(value: Any, source_type: str = "explicit") -> list[dict[str, Any]]:
    """Normalize explicit refs while rejecting quotes/metadata without identity."""
    values = value if isinstance(value, list) else [value]
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str):
            source_id = item.strip()
            if not source_id or len(source_id) > 240 or source_id.startswith(("{", "[")):
                continue
            reference = {"source_id": source_id, "source_type": source_type}
        elif isinstance(item, Mapping):
            reference = _source_reference(item, str(item.get("source_type") or source_type), quote=item.get("quote"))
            if reference is None:
                continue
        else:
            continue
        marker = json.dumps(reference, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            references.append(reference)
    return references


def _source_index(understanding: Any, units: Any) -> list[dict[str, Any]]:
    source_records = _source_records(units, "source_unit")
    if source_records:
        return source_records
    return _source_records(understanding, "tender_understanding")


def _term_present(text: str, term: str) -> bool:
    term = " ".join(term.casefold().split())
    text = " ".join(text.casefold().split())
    if not term:
        return False
    if len(term) <= 3 and term.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    if term in text:
        return True
    # PDF table extraction can split a word at a visual column or line edge.
    # Keep exact token matching first, then allow only a compact multi-word
    # phrase match so layout noise does not silently disable an applicable row.
    if " " not in term:
        return False
    compact_term = re.sub(r"[^a-z0-9]", "", term)
    compact_text = re.sub(r"[^a-z0-9]", "", text)
    return len(compact_term) >= 8 and compact_term in compact_text


def _matching_references(item: Mapping[str, Any], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terms = [str(term) for term in item.get("source_terms", []) if term]
    if not terms:
        return []
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        if any(_term_present(source["text"], term) for term in terms):
            reference = deepcopy(source["reference"])
            reference.setdefault("quote", source["text"][:1000])
            marker = json.dumps(reference, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                matches.append(reference)
        if len(matches) >= 8:
            break
    return matches


def _explicit_sources(item: Mapping[str, Any]) -> list[Any]:
    for key in (
        "source_references",
        "source_reference",
        "source_refs",
        "rfp_source_refs",
        "references",
        "citations",
        "source_ids",
        "evidence_source_ids",
        "source_pages",
    ):
        value = item.get(key)
        if value not in (None, "", [], {}):
            return _normalize_source_references(value)
    return []


def _dynamic_source_refs(item: Mapping[str, Any], sources: list[dict[str, Any]]) -> list[Any]:
    explicit = _explicit_sources(item)
    if explicit:
        return deepcopy(explicit)
    search = " ".join(
        str(item.get(key) or "")
        for key in ("title", "label", "name", "requirement", "description")
    ).strip()
    words = [word for word in re.findall(r"[a-z0-9]{4,}", search.casefold()) if word not in {"must", "shall", "should", "required"}]
    if not words:
        return []
    matches: list[Any] = []
    for source in sources:
        if sum(_term_present(source["text"], word) for word in words) >= max(1, min(2, len(words))):
            matches.append(deepcopy(source["reference"]))
        if len(matches) >= 8:
            break
    return matches


def _source_grounded(item: Mapping[str, Any], sources: list[dict[str, Any]]) -> tuple[bool, list[Any]]:
    explicit = _explicit_sources(item)
    if explicit:
        return True, explicit
    refs = _dynamic_source_refs(item, sources)
    return bool(refs), refs
