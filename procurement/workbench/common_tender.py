"""Tender ledger, provenance binding, and checklist-draft projection."""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from procurement.domain.models import new_id
from procurement.understanding.service import summary_from_resolved_fields
from procurement.workbench.common_contracts import _normalize_contract_draft


_TENDER_CONTEXT_FIELD_KEYS = (
    "tender_identity",
    "tender_subject_classification",
    "estimated_procurement_value",
    "tender_timeline",
    "work_delivery_location",
    "evaluation_method",
    "bid_structure_and_auction",
    "evaluation_scoring",
    "highest_bid_elimination",
)


def _ledger(data: dict[str, Any]) -> dict[str, Any]:
    reviewed = data.get("reviewed_ledger")
    if isinstance(reviewed, dict) and isinstance(reviewed.get("entries"), list):
        return reviewed
    field_understanding = data.get("field_understanding")
    if isinstance(field_understanding, dict) and isinstance(field_understanding.get("sections"), list):
        units = data.get("source_units") or {}
        entries = []
        for section in field_understanding["sections"]:
            for field in section.get("fields") or []:
                evidence = []
                for reference in field.get("evidence") or []:
                    source_id = str(reference.get("source_id") or "")
                    unit = units.get(source_id) or {}
                    evidence.append(
                        {
                            "source_id": source_id,
                            "document_id": reference.get("document_id") or unit.get("document_id") or unit.get("document") or "",
                            "page_id": reference.get("page_id") or unit.get("page_id") or "",
                            "page": reference.get("page") or unit.get("page"),
                            "quote": reference.get("quote") or "",
                            "page_image_url": None,
                        }
                    )
                verified = field.get("audit", {}).get("verified") is True
                blocking = [
                    str(item)
                    for item in field.get("audit", {}).get("blocking_grounding_issues") or []
                ]
                entries.append(
                    {
                        "key": str(field.get("key") or ""),
                        "label": str(field.get("label") or field.get("key") or ""),
                        "requirement_level": "reference",
                        "status": str(field.get("status") or "unresolved"),
                        "value": field.get("value"),
                        "conditions": [str(item) for item in field.get("conditions") or []],
                        "exceptions": [str(item) for item in field.get("exceptions") or []],
                        "required_documents": [str(item) for item in field.get("required_documents") or []],
                        "evidence": evidence,
                        "verified": verified,
                        "review_source": "ai_extracted",
                        "blocking_grounding_issues": blocking,
                        "notes": str(field.get("notes") or ""),
                    }
                )
        by_key = {item["key"]: item for item in entries}
        summary = summary_from_resolved_fields(field_understanding) or by_key.get("tender_summary", {}).get("value")
        key_facts = []
        for key in ("tender_title", "tender_id", "organisation", "bid_submission_end_date", "emd_amount_inr"):
            item = by_key.get(key)
            if item and item.get("value") not in (None, "", []):
                key_facts.append({"label": item["label"], "value": str(item["value"])})
        needs_review = any(
            item["status"] in {"ambiguous", "need_more_evidence", "unresolved"}
            or item["blocking_grounding_issues"]
            for item in entries
        )
        return {
            "ledger_status": "needs_review" if needs_review else "complete",
            "editable": True,
            "tender_summary": {
                "text": str(summary or "Tender understanding is available for review."),
                "source": "ai_extracted",
                "key_facts": key_facts,
            },
            "entries": entries,
            "warnings": data.get("warnings") or [],
            "raw_tender": data.get("tender_profile") or {},
        }
    profile = data.get("tender_profile") or {}
    extraction = data.get("tender_extraction") or {}
    tender = extraction.get("tender") or {}
    entries: list[dict[str, Any]] = []
    identity_fields = (
        ("tender_title", "Tender title", profile.get("tender_title")),
        ("tender_number", "Tender number", profile.get("tender_number")),
        ("buyer", "Buyer", profile.get("buyer")),
        ("procurement_type", "Procurement type", profile.get("procurement_type")),
        ("bid_system", "Bid system", profile.get("bid_system")),
        ("scope", "Scope", profile.get("scope")),
        ("evaluation_method", "Evaluation method", profile.get("evaluation_method")),
    )
    known_values = {key: value for key, _label, value in identity_fields}
    known_values.update(
        tender_id=profile.get("tender_number"),
        organisation=profile.get("buyer"),
        organization=profile.get("buyer"),
        tender_summary=profile.get("scope"),
    )
    schema = data.get("field_schema")
    schema_fields = [
        field
        for section in (schema.get("sections") or [])
        for field in (section.get("fields") or [])
    ] if isinstance(schema, dict) else []
    projected_fields = (
        ((str(field.get("key") or ""), str(field.get("label") or field.get("key") or ""), known_values.get(str(field.get("key") or ""))) for field in schema_fields)
        if schema_fields
        else identity_fields
    )
    for key, label, value in projected_fields:
        entries.append(
            {
                "key": key,
                "label": label,
                "requirement_level": "reference",
                "status": "found" if value not in (None, "", "Unknown") else "not_found",
                "value": value,
                "conditions": [],
                "exceptions": [],
                "required_documents": [],
                "evidence": [],
                "verified": bool(value not in (None, "", "Unknown")),
                "review_source": "ai_extracted",
                "blocking_grounding_issues": [],
            }
        )
    for index, requirement in enumerate(extraction.get("requirements") or [], start=1):
        refs = [
            {
                "document_id": ref.get("document_id") or "",
                "page": ref.get("page_number"),
                "quote": ref.get("quote") or "",
                "page_image_url": None,
            }
            for ref in requirement.get("source_refs") or []
        ]
        entries.append(
            {
                "key": requirement.get("requirement_id") or f"requirement_{index}",
                "label": requirement.get("requirement") or f"Tender requirement {index}",
                "requirement_level": "always_required" if requirement.get("mandatory", True) else "conditional",
                "status": "found" if refs else "need_more_evidence",
                "value": requirement.get("requirement"),
                "conditions": [],
                "exceptions": [],
                "required_documents": [
                    item.get("display_name") or item.get("canonical_key")
                    for item in requirement.get("required_documents") or []
                ],
                "evidence": refs,
                "verified": bool(refs and all(ref.get("quote") for ref in refs)),
                "review_source": requirement.get("review_source") or "ai_extracted",
                "blocking_grounding_issues": [] if refs else ["source evidence is missing"],
            }
        )
    summary_text = " â€” ".join(
        str(value) for value in (profile.get("tender_title"), profile.get("buyer"), profile.get("scope")) if value and value != "Unknown"
    )
    return {
        "ledger_status": "complete" if entries and not any(item["status"] in {"unresolved", "need_more_evidence"} for item in entries) else "needs_review",
        "editable": True,
        "tender_summary": {
            "text": summary_text or "Tender understanding is available for review.",
            "source": "ai_extracted",
            "key_facts": [
                {"label": label, "value": str(value)}
                for _key_name, label, value in identity_fields[:5]
                if value not in (None, "", "Unknown")
            ],
        },
        "entries": entries,
        "warnings": data.get("warnings") or [],
        "raw_tender": tender,
    }


def _contract_tender_context(data: Mapping[str, Any]) -> dict[str, Any]:
    ledger = _ledger(dict(data))
    entries = {
        str(item.get("key") or ""): item
        for item in ledger.get("entries") or []
        if isinstance(item, Mapping)
    }
    return {
        key: {
            "status": entries[key].get("status"),
            "value": copy.deepcopy(entries[key].get("value")),
            "conditions": copy.deepcopy(entries[key].get("conditions") or []),
            "exceptions": copy.deepcopy(entries[key].get("exceptions") or []),
            "evidence": copy.deepcopy(entries[key].get("evidence") or []),
        }
        for key in _TENDER_CONTEXT_FIELD_KEYS
        if key in entries
    }


def _reference_list(value: Any) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _source_unit_parts(unit: Mapping[str, Any]) -> tuple[str, str, int | None, str, str]:
    document_id = str(
        unit.get("document_id")
        or unit.get("documentId")
        or unit.get("document")
        or ""
    ).strip()
    page_id = str(unit.get("page_id") or unit.get("pageId") or "").strip()
    page_value = unit.get("page") or unit.get("page_number") or unit.get("pageNumber")
    page: int | None
    try:
        page = int(page_value) if page_value not in (None, "") else None
    except (TypeError, ValueError):
        page = None
    source_file = str(
        unit.get("source_file")
        or unit.get("file_name")
        or unit.get("filename")
        or ""
    ).strip()
    text = str(
        unit.get("text")
        or unit.get("source_text")
        or unit.get("content")
        or unit.get("quote")
        or ""
    ).strip()
    return document_id, page_id, page, source_file, text


def _source_units(data: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], list[tuple[str, Mapping[str, Any]]]]:
    raw = data.get("source_units") or {}
    if not isinstance(raw, Mapping):
        return {}, []
    lookup: dict[str, Mapping[str, Any]] = {}
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for source_key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        source_id = str(source_key).strip()
        entries.append((source_id, value))
        aliases = (
            source_id,
            str(value.get("source_id") or value.get("sourceId") or "").strip(),
            str(value.get("unit_id") or value.get("unitId") or "").strip(),
            str(value.get("page_id") or value.get("pageId") or "").strip(),
        )
        for alias in aliases:
            if alias:
                lookup.setdefault(alias, value)
    return lookup, entries


def _reference_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _reference_page(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        match = re.search(r"\b(?:page|p)[ .:_-]*(\d+)\b", str(value), re.IGNORECASE)
        page = int(match.group(1)) if match else None
    return page if page and page > 0 else None


def _reference_source_id(reference: Mapping[str, Any]) -> str:
    return str(
        reference.get("source_id")
        or reference.get("sourceId")
        or reference.get("unit_id")
        or reference.get("unitId")
        or ""
    ).strip()


def _find_source_unit(
    reference: Mapping[str, Any],
    unit_lookup: Mapping[str, Mapping[str, Any]],
    unit_entries: list[tuple[str, Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    source_id = _reference_source_id(reference)
    if source_id and source_id in unit_lookup:
        return unit_lookup[source_id]

    page_id = str(reference.get("page_id") or reference.get("pageId") or "").strip()
    if page_id and page_id in unit_lookup:
        return unit_lookup[page_id]

    requested_document = str(
        reference.get("document_id")
        or reference.get("documentId")
        or reference.get("document")
        or ""
    ).strip()
    requested_page = _reference_page(
        reference.get("page") or reference.get("page_number") or reference.get("pageNumber")
    )
    requested_file = str(
        reference.get("source_file")
        or reference.get("file_name")
        or reference.get("filename")
        or ""
    ).strip().casefold()
    requested_quote = _reference_text(
        reference.get("quote")
        or reference.get("excerpt")
        or reference.get("snippet")
    ).casefold()

    candidates: list[Mapping[str, Any]] = []
    for _source_key, unit in unit_entries:
        document_id, unit_page_id, page, source_file, text = _source_unit_parts(unit)
        if requested_document and document_id != requested_document:
            continue
        if requested_page is not None and page != requested_page:
            continue
        if requested_file and source_file.casefold() != requested_file:
            continue
        if page_id and unit_page_id != page_id:
            continue
        if requested_quote and requested_quote not in _reference_text(text).casefold():
            continue
        candidates.append(unit)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _contract_source_ref(
    value: Any,
    unit_lookup: Mapping[str, Mapping[str, Any]],
    unit_entries: list[tuple[str, Mapping[str, Any]]],
    fallback_quote: str = "",
) -> dict[str, Any] | None:
    if isinstance(value, str):
        reference: Mapping[str, Any] = {"source_id": value.strip()}
    elif isinstance(value, Mapping):
        reference = value
    else:
        return None

    unit = _find_source_unit(reference, unit_lookup, unit_entries)
    unit_document, unit_page_id, unit_page, _source_file, unit_text = _source_unit_parts(unit or {})
    explicit_document = str(
        reference.get("document_id")
        or reference.get("documentId")
        or reference.get("document")
        or ""
    ).strip()
    document_id = explicit_document or unit_document
    if document_id.casefold() == "tender":
        document_id = unit_document if unit_document.casefold() != "tender" else ""
    if not document_id:
        return None

    page = _reference_page(
        reference.get("page") or reference.get("page_number") or reference.get("pageNumber")
    ) or unit_page
    page_id = str(reference.get("page_id") or reference.get("pageId") or unit_page_id).strip()
    if page is None:
        page = _reference_page(page_id)
    quote = _reference_text(
        reference.get("quote")
        or reference.get("excerpt")
        or reference.get("snippet")
        or unit_text
        or (fallback_quote if (_reference_source_id(reference) or page_id or page is not None) else "")
    )
    if not quote or (page is None and not page_id and not _reference_source_id(reference)):
        return None

    return {"document_id": document_id, "page": page, "quote": quote}


def _field_evidence(data: Mapping[str, Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    understanding = data.get("field_understanding") or {}
    if isinstance(understanding, Mapping):
        for section in understanding.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            for field in section.get("fields") or []:
                if isinstance(field, Mapping) and field.get("key"):
                    result.setdefault(str(field["key"]), []).extend(_reference_list(field.get("evidence")))
    reviewed = data.get("reviewed_ledger") or {}
    if isinstance(reviewed, Mapping):
        for entry in reviewed.get("entries") or []:
            if isinstance(entry, Mapping) and entry.get("key"):
                result.setdefault(str(entry["key"]), []).extend(_reference_list(entry.get("evidence")))
    return result


def _criterion_aliases(value: Any) -> set[str]:
    text = str(value or "").strip().casefold()
    if not text:
        return set()
    return {text, text.removeprefix("bhel-")}


def _matrix_source_candidates(
    matrix: Mapping[str, Any],
    item: Mapping[str, Any],
    criterion_id: str,
) -> list[Any]:
    candidates: list[Any] = []

    def add(value: Any) -> None:
        candidates.extend(_reference_list(value))

    for key in ("rfp_source_refs", "source_references", "source_refs", "citations"):
        add(item.get(key))
    extensions = item.get("extensions")
    if isinstance(extensions, Mapping):
        for extension in (
            extensions.get("bhel"),
            extensions.get("provenance"),
            extensions,
        ):
            if isinstance(extension, Mapping):
                for key in ("rfp_source_refs", "source_references", "source_refs", "citations"):
                    add(extension.get(key))

    matrix_extensions = matrix.get("extensions")
    bhel = matrix_extensions.get("bhel") if isinstance(matrix_extensions, Mapping) else None
    if not isinstance(bhel, Mapping):
        bhel = matrix.get("bhel") if isinstance(matrix.get("bhel"), Mapping) else None
    if not isinstance(bhel, Mapping):
        return candidates
    aliases = _criterion_aliases(criterion_id)
    for key in ("source_references", "source_refs", "citations"):
        for reference in _reference_list(bhel.get(key)):
            if not isinstance(reference, Mapping):
                continue
            reference_ids: set[str] = set()
            for id_key in ("criterion_id", "check_id", "id", "key"):
                reference_ids.update(_criterion_aliases(reference.get(id_key)))
            if reference_ids and aliases & reference_ids:
                add(reference)
    for key in ("baseline_checks", "bidder_checks", "dynamic_checks"):
        for check in bhel.get(key) or []:
            if not isinstance(check, Mapping):
                continue
            check_ids = set()
            for id_key in ("criterion_id", "check_id", "id", "key"):
                check_ids.update(_criterion_aliases(check.get(id_key)))
            if aliases & check_ids:
                for ref_key in ("rfp_source_refs", "source_references", "source_refs", "citations"):
                    add(check.get(ref_key))
    return candidates


def _draft(data: dict[str, Any]) -> dict[str, Any]:
    existing = data.get("contract_draft")
    if data.get("frozen_contract") and isinstance(existing, dict) and existing.get("criteria") is not None:
        # Approved legacy and current contracts are immutable byte-for-byte.
        return copy.deepcopy(existing)
    if (
        isinstance(existing, dict)
        and existing.get("criteria") is not None
        and existing.get("schema_version") == "evaluation-checklist.v1"
    ):
        # Once created, the reviewer-visible draft is the source of truth.
        # Ledger corrections explicitly clear it and therefore trigger a rebuild.
        normalized = _normalize_contract_draft(existing)
        normalized.setdefault("tender_context", _contract_tender_context(data))
        return normalized
    matrix = data.get("requirement_matrix") or {}
    if not isinstance(matrix, Mapping) or not isinstance(matrix.get("items"), list):
        if isinstance(existing, dict) and existing.get("criteria") is not None:
            legacy = copy.deepcopy(existing)
            for criterion in legacy.get("criteria") or []:
                if not isinstance(criterion, dict):
                    continue
                criterion["missing_fields"] = [
                    "field:" + str(item).removeprefix("rule:FIELD-")
                    if str(item).startswith("rule:FIELD-")
                    else str(item)
                    for item in criterion.get("missing_fields") or []
                ]
            return _normalize_contract_draft(legacy)
        return _normalize_contract_draft({"draft_id": "", "criteria": [], "unautomated_requirements": [], "planning_notes": []})

    unit_lookup, unit_entries = _source_units(data)
    field_evidence = _field_evidence(data)
    reviewed_ledger = data.get("reviewed_ledger") or {}
    reviewed = {
        str(entry.get("key") or ""): entry
        for entry in (reviewed_ledger.get("entries") if isinstance(reviewed_ledger, Mapping) else []) or []
        if isinstance(entry, Mapping)
    }
    criteria = []
    for item in matrix.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        criterion_id = str(item.get("criterion_id") or "")
        field_key = str(item.get("field_key") or "")
        if not field_key and criterion_id.startswith("FIELD-"):
            field_key = criterion_id.removeprefix("FIELD-")
        finding = reviewed.get(field_key) or {}
        human_corrected = finding.get("review_source") == "human_corrected"
        if human_corrected and finding.get("status") in {"not_found", "not_applicable"}:
            continue
        evidence = item.get("required_evidence") or []
        source_quote = item.get("source_quote") or ""
        extensions = item.get("extensions")
        extensions = extensions if isinstance(extensions, Mapping) else {}
        compiler_provenance = extensions.get("provenance")
        has_compiler_provenance = isinstance(compiler_provenance, Mapping)
        human_resolved = human_corrected and finding.get("status") == "found"
        source_candidates = [
            *_matrix_source_candidates(matrix, item, criterion_id),
            *([] if has_compiler_provenance else field_evidence.get(field_key, [])),
            *(
                _reference_list(finding.get("evidence"))
                if human_resolved or not has_compiler_provenance
                else []
            ),
        ]
        source_refs = []
        seen_refs: set[tuple[str, Any, str]] = set()
        for reference in source_candidates:
            normalized = _contract_source_ref(
                reference,
                unit_lookup,
                unit_entries,
                fallback_quote=source_quote,
            )
            if not normalized:
                continue
            marker = (
                str(normalized["document_id"]),
                normalized.get("page"),
                str(normalized.get("quote") or ""),
            )
            if marker in seen_refs:
                continue
            seen_refs.add(marker)
            source_refs.append(normalized)
        bhel_extension = extensions.get("bhel") if isinstance(extensions, Mapping) else {}
        bhel_extension = bhel_extension if isinstance(bhel_extension, Mapping) else {}
        evaluation_extension = extensions.get("evaluation") if isinstance(extensions, Mapping) else {}
        evaluation_extension = evaluation_extension if isinstance(evaluation_extension, Mapping) else {}
        applicability = str(
            item.get("applicability")
            or evaluation_extension.get("applicability")
            or (bhel_extension.get("applicability") if isinstance(bhel_extension, Mapping) else "")
            or "applicable"
        ).casefold()
        missing = list(item.get("missing_fields") or [])
        missing += [] if source_refs or human_resolved or applicability == "not_applicable" else [
            f"field:{field_key} (quote cannot be grounded)"
            if field_key
            else f"rule:{criterion_id or 'criterion'} (quote cannot be grounded)"
        ]
        requirement = item.get("requirement") or "Tender requirement"
        if human_resolved:
            requirement = f"{finding.get('label') or field_key}: {finding.get('value')}"
        criteria.append(
            {
                "criterion_id": criterion_id or new_id(),
                "title": requirement,
                "family": item.get("category") or "other",
                "rule": {
                    "rule_family": item.get("category") or "other",
                    "requirement": requirement,
                    "mandatory": bool(item.get("mandatory", True)),
                    "failure_effect": item.get("failure_effect") or "manual_review",
                    "pass_rule": item.get("pass_rule") or "all_required_evidence_must_pass",
                    "required_documents": (
                        list(finding.get("required_documents") or [])
                        if human_resolved
                        else [row.get("document_name") or row.get("evidence_id") for row in evidence]
                    ),
                },
                "rfp_source_refs": source_refs,
                "required_evidence": copy.deepcopy(evidence),
                "evidence_groups": copy.deepcopy(item.get("evidence_groups") or []),
                "missing_fields": missing,
                "needs_human_input": bool(missing),
                "depends_on": [],
                "notes": "Resolved by reviewed human correction." if human_resolved else "",
                "extensions": dict(extensions) if isinstance(extensions, Mapping) else {},
            }
        )
    return _normalize_contract_draft({
        "draft_id": "",
        "criteria": criteria,
        "tender_context": _contract_tender_context(data),
        "unautomated_requirements": copy.deepcopy(matrix.get("unautomated_requirements") or []),
        "planning_notes": matrix.get("planning_notes") or [],
    })
