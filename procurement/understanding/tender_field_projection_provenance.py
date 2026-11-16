"""Canonical tender source references and matrix provenance."""

from __future__ import annotations

from typing import Any


_SOURCE_REFERENCE_METADATA = {
    "authority_tier": ("authority_tier", "authorityTier"),
    "authority_rank": ("authority_rank", "authorityRank"),
    "revision_sequence": ("revision_sequence", "revisionSequence"),
    "revision_number": ("revision_number", "revisionNumber"),
    "revision_body_number": ("revision_body_number", "revisionBodyNumber"),
    "revision_filename_number": (
        "revision_filename_number",
        "revisionFilenameNumber",
    ),
    "revision_number_source": ("revision_number_source", "revisionNumberSource"),
    "revision_date": ("revision_date", "revisionDate"),
    "revision_metadata_complete": (
        "revision_metadata_complete",
        "revisionMetadataComplete",
    ),
    "revision_order_warning": ("revision_order_warning", "revisionOrderWarning"),
    "chronology_basis": ("chronology_basis", "chronologyBasis"),
    "effective_state": ("effective_state", "effectiveState"),
    "superseded_by_document_id": (
        "superseded_by_document_id",
        "supersededByDocumentId",
    ),
    "superseded_by_scope": ("superseded_by_scope", "supersededByScope"),
    "section_scope": ("section_scope", "sectionScope"),
    "replacement_scope": ("replacement_scope", "replacementScope"),
    "formal_revision": ("formal_revision", "formalRevision"),
    "formal_revision_scope": ("formal_revision_scope", "formalRevisionScope"),
    "embedded_revision_fragment": (
        "embedded_revision_fragment",
        "embeddedRevisionFragment",
    ),
    "amendment_candidate": ("amendment_candidate", "amendmentCandidate"),
    "amendment_relevance_score": (
        "amendment_relevance_score",
        "amendmentRelevanceScore",
    ),
}


def _provenance_value(
    reference: dict[str, Any],
    unit: dict[str, Any],
    *keys: str,
) -> Any:
    for source in (reference, unit):
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _source_unit_for_reference(
    reference: dict[str, Any],
    units: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    source_ids = [
        str(reference.get(key) or "").strip()
        for key in ("source_id", "sourceId", "unit_id", "unitId")
        if str(reference.get(key) or "").strip()
    ]
    for source_id in source_ids:
        unit = units.get(source_id)
        if isinstance(unit, dict):
            return source_id, unit
    for source_id in source_ids:
        for unit_id, unit in units.items():
            if not isinstance(unit, dict):
                continue
            if any(
                str(unit.get(key) or "").strip() == source_id
                for key in ("source_id", "sourceId", "unit_id", "unitId")
            ):
                return str(unit_id), unit

    page_id = str(
        reference.get("page_id")
        or reference.get("pageId")
        or ""
    ).strip()
    candidates = [
        (str(unit_id), unit)
        for unit_id, unit in units.items()
        if isinstance(unit, dict)
        and page_id
        and str(unit.get("page_id") or unit.get("pageId") or "").strip() == page_id
    ]
    if candidates:
        quote = str(reference.get("quote") or reference.get("text") or "").strip()
        if quote:
            for unit_id, unit in candidates:
                if quote in str(unit.get("text") or ""):
                    return unit_id, unit
        return candidates[0]
    return "", {}


def _canonical_source_reference(
    reference: Any,
    units: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(reference, dict):
        return None
    unit_id, unit = _source_unit_for_reference(reference, units)
    has_source_or_quote = any(
        str(reference.get(name) or "").strip()
        for name in ("source_id", "sourceId", "unit_id", "unitId", "quote", "text", "source_quote")
    )
    page_only_visual = not has_source_or_quote and (
        bool(reference.get("visual_evidence"))
        or (
            any(name in reference for name in ("source_id", "sourceId", "unit_id", "unitId"))
            and any(name in reference for name in ("quote", "text", "source_quote"))
            and any(reference.get(name) not in (None, "") for name in ("page_id", "pageId", "page", "page_number"))
        )
    )
    source_id = str(
        ""
        if page_only_visual
        else (
            _provenance_value(reference, unit, "source_id", "sourceId", "unit_id", "unitId")
            or unit_id
            or ""
        )
    ).strip()
    document_id = str(
        _provenance_value(
            reference,
            unit,
            "document_id",
            "documentId",
            "source_document_id",
            "sourceDocumentId",
            "document",
        )
        or ""
    ).strip()
    page_id = str(
        _provenance_value(reference, unit, "page_id", "pageId") or ""
    ).strip()
    page = _provenance_value(reference, unit, "page", "page_number", "pageNumber")
    quote = str(
        ""
        if page_only_visual
        else (_provenance_value(reference, unit, "quote", "text", "source_quote") or "")
    ).strip()
    source_file = str(
        _provenance_value(
            reference,
            unit,
            "source_file",
            "sourceFile",
            "file_name",
            "filename",
        )
        or ""
    ).strip()
    source_role = str(
        _provenance_value(reference, unit, "source_role", "sourceRole") or "unknown"
    ).strip()
    if not any((source_id, document_id, page_id, page not in (None, ""), quote)):
        return None

    canonical: dict[str, Any] = {
        "source_id": source_id or None,
        "document_id": document_id or None,
        "page_id": page_id or None,
        "page": page,
        "quote": quote or None,
        "source_file": source_file or None,
        "source_role": source_role or "unknown",
    }
    for name, aliases in _SOURCE_REFERENCE_METADATA.items():
        value = _provenance_value(reference, unit, *aliases)
        if value not in (None, "", [], {}):
            canonical[name] = value
    for name in ("clause_number", "section_id", "section_path", "unit_type", "table"):
        value = _provenance_value(reference, unit, name)
        if value not in (None, "", [], {}):
            canonical[name] = value
    return canonical


def _canonical_source_references(
    evidence: Any,
    units: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        return []
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in evidence:
        reference = _canonical_source_reference(item, units)
        if not reference:
            continue
        marker = (
            str(reference.get("source_id") or ""),
            str(reference.get("document_id") or ""),
            str(reference.get("page_id") or ""),
            str(reference.get("page") or ""),
            str(reference.get("quote") or ""),
        )
        if marker in seen:
            continue
        seen.add(marker)
        references.append(reference)
    return references


def _canonical_source_units(units: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(units, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for unit_id, value in units.items():
        if not isinstance(value, dict):
            continue
        canonical_id = str(
            value.get("source_id")
            or value.get("sourceId")
            or value.get("unit_id")
            or value.get("unitId")
            or unit_id
        ).strip()
        result[str(unit_id)] = {**value, "source_id": canonical_id}
    return result


def _attach_matrix_provenance(
    matrix: dict[str, Any],
    units: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    for item in matrix.get("items") or []:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        provenance = extensions.get("provenance")
        provenance = dict(provenance) if isinstance(provenance, dict) else {}
        candidates: list[Any] = []
        for value in (
            item.get("source_references"),
            item.get("source_refs"),
            provenance.get("source_references"),
            (extensions.get("bhel") or {}).get("source_references")
            if isinstance(extensions.get("bhel"), dict)
            else None,
        ):
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
        references = _canonical_source_references(candidates, units)
        item["source_references"] = references
        provenance["source_references"] = references
        provenance["status"] = "grounded" if references else "missing"
        provenance["source_reference_count"] = len(references)
        extensions["provenance"] = provenance
        item["extensions"] = extensions
        if not item.get("source_quote") and references:
            item["source_quote"] = references[0].get("quote")
    return matrix
