"""Amendment targeting, field linkage, and visual follow-up selection."""

from __future__ import annotations

import re
from typing import Any

from .field_binding import (
    _binding_text_match,
    _direct_scalar_update_value,
    _field_presence_match,
    _field_subject_compatible,
    _needs_gate_lane,
    _requires_exhaustive_coverage,
)
from .field_units import (
    _clause_locator_keys,
    _formal_revision_cells,
    _is_formal_revision_row,
    _issuer_response_text,
    _reference_keys,
    _table_cells,
    _table_header_index,
    _unit_clause_locator_keys,
    _unit_page,
    _unit_text,
)
from .lexical import _WORD_RE, _compact_text, _phrase_text, _tokens
from .schema import RfpFieldSpec


_UPDATE_QUERY = "revised clause corrigendum addendum amendment clarification bank response"

_EXPLICIT_UPDATE_CUES = (
    "stands deleted",
    "revised clause",
    "replaced by",
    "instead of",
    "shall be read as",
    "modified as",
    "amended as",
)

_DELETE_CUES = (
    "stands deleted",
    "stand deleted",
    "is deleted",
    "shall be deleted",
    "is hereby deleted",
    "clause is removed",
    "clause stands removed",
)


def _formal_revision_target_text(unit: dict[str, Any]) -> str:
    cells = _table_cells(unit) if "table_cells" not in unit else list(unit.get("table_cells") or [])
    headers = [str(item) for item in unit.get("table_headers") or []]
    existing_index = _table_header_index(headers, "existing", "current clause")
    revised_index = _table_header_index(
        headers,
        "revised",
        "modified clause",
        "amended clause",
    )
    excluded = {
        index
        for index in (existing_index, revised_index)
        if index is not None and index < len(cells)
    }
    target_cells = [cell for index, cell in enumerate(cells) if index not in excluded]
    if not excluded and len(cells) >= 3:
        target_cells = cells[:-2]
    return _compact_text(
        " | ".join(
            [str(unit.get("clause_number") or ""), *[str(cell) for cell in target_cells]]
        )
    )


def _table_binding_target_text(unit: dict[str, Any]) -> str:
    cells = _table_cells(unit) if "table_cells" not in unit else list(unit.get("table_cells") or [])
    headers = [str(item) for item in unit.get("table_headers") or []]
    if not cells or len(cells) != len(headers):
        return ""
    excluded = (
        "response",
        "proof",
        "evidence",
        "compliance",
        "document to be submitted",
        "max mark",
        "maximum mark",
        "score",
        "bidder query",
        "bidder request",
        "request",
        "points of clarification",
        "remarks",
    )
    selected = [
        cell
        for header, cell in zip(headers, cells)
        if not any(cue in _phrase_text(header) for cue in excluded)
    ]
    return _compact_text(" | ".join(str(cell) for cell in selected))


def _formal_gate_revision_match(
    field: RfpFieldSpec,
    revision: tuple[str, str],
) -> bool:
    if not _needs_gate_lane(field):
        return False
    field_text = _phrase_text(f"{field.key} {field.label} {field.description}")
    if "technical" not in field_text:
        return False
    revised = _phrase_text(revision[1])
    return bool(
        re.search(
            r"(?:minimum|qualifying).{0,80}(?:technical|qualification)"
            r".{0,80}(?:marks|score|threshold|percent)|"
            r"(?:technical|qualification).{0,80}(?:minimum|qualifying)"
            r".{0,80}(?:marks|score|threshold|percent)",
            revised,
            re.I,
        )
    )


def _field_target_match(
    unit: dict[str, Any],
    field: RfpFieldSpec,
    formal_revision_sections: set[str],
) -> bool:
    if not _is_formal_revision_row(unit, formal_revision_sections):
        target_text = (
            _table_binding_target_text(unit)
            if unit.get("unit_type") in {"table_row", "table_header"}
            else _unit_text(unit)
        )
        return bool(
            target_text
            and _binding_text_match(target_text, field)
            and _field_subject_compatible(field, target_text)
        )
    if _formal_revision_cells(unit) is None:
        return False
    revision = _formal_revision_cells(unit)
    previous_absent = _phrase_text(revision[0]) in {
        "",
        "nil",
        "none",
        "not applicable",
        "new clause",
        "additional clause",
    }
    if (
        previous_absent
        and _binding_text_match(revision[1], field)
        and _field_subject_compatible(field, revision[1])
    ):
        return True
    target_text = _formal_revision_target_text(unit)
    return bool(
        (
            _binding_text_match(target_text, field)
            and _field_subject_compatible(
                field,
                f"{target_text} {revision[0]} {revision[1]}",
            )
        )
        or (
            revision
            and _formal_gate_revision_match(field, revision)
            and _field_subject_compatible(field, f"{revision[0]} {revision[1]}")
        )
    )


def _formal_revision_binds_field(
    field: RfpFieldSpec,
    item: dict[str, Any],
    retriever: Any,
) -> bool:
    revision = _formal_revision_cells(item)
    if not revision:
        return False
    target_text = _formal_revision_target_text(item)
    combined = f"{target_text} {revision[0]} {revision[1]}"
    if _binding_text_match(target_text, field):
        return _field_subject_compatible(field, combined)
    target_locators = _clause_locator_keys(target_text)
    if target_locators:
        linked_bases = [
            unit
            for unit in retriever.units.values()
            if unit.get("source_role") == "base_rfp"
            and target_locators & _unit_clause_locator_keys(unit)
        ]
        if linked_bases:
            return any(
                _field_presence_match(unit, field)
                and _field_subject_compatible(field, f"{_unit_text(unit)} {combined}")
                for unit in linked_bases
            )
        return False
    if _phrase_text(revision[0]) in {
        "",
        "nil",
        "none",
        "not applicable",
        "new clause",
        "additional clause",
    } and _binding_text_match(revision[1], field):
        return _field_subject_compatible(field, combined)
    if target_text:
        return False
    return _binding_text_match(revision[0], field) and (
        _field_subject_compatible(field, combined)
    )


def _is_layout_sensitive_evidence(item: dict[str, Any]) -> bool:
    return bool(
        item.get("unit_type") in {"table", "table_row", "table_cell"}
        or item.get("table_headers")
        or item.get("table_continuation_source_ids")
        or item.get("revision_layout")
        or item.get("formal_revision")
    )


def _binding_update_strength(item: dict[str, Any]) -> int:
    response = _phrase_text(_issuer_response_text(item))
    if response and any(cue in response for cue in _EXPLICIT_UPDATE_CUES):
        return 4
    if response and (
        _pointer_only_amendment_response(response) or "no change" in response
    ):
        # Some issuers combine a retain instruction with a second, material
        # clarification in the same response cell. Keep that row auditable.
        if len(_WORD_RE.findall(response)) > 12 and any(
            cue in f" {response} "
            for cue in (" shall ", " must ", " only ", " required ", " deleted ")
        ):
            return 2
        return 0
    existing = _phrase_text(str(item.get("existing_text") or ""))
    revised = _phrase_text(str(item.get("revised_text") or ""))
    if existing and revised and existing != revised:
        return 3
    text = _phrase_text(str(item.get("text") or ""))
    if any(cue in text for cue in _EXPLICIT_UPDATE_CUES) and (
        item.get("amendment_candidate")
        or item.get("direct_field_match")
        or item.get("anchor_match")
        or item.get("formal_revision")
        or item.get("formal_revision_scope")
    ):
        return 2
    return 1 if response else 0


def _unaccounted_binding_amendment_source_ids(
    evidence: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[str]:
    represented = {
        str(source_id)
        for event in events
        for source_id in event.get("source_ids") or []
    }
    missing: list[str] = []
    for item in evidence:
        source_id = str(item.get("source_id") or "")
        if (
            not source_id
            or item.get("source_role") != "post_base_update"
            or item.get("effective_state") == "superseded"
            or item.get("embedded_revision_fragment")
            or not item.get("amendment_candidate")
            or _binding_update_strength(item) < 2
            or not _amendment_target_links_base_evidence(item, evidence)
        ):
            continue
        linked_ids = {
            source_id,
            *[str(value) for value in item.get("revision_source_ids") or []],
            *[
                str(value)
                for value in item.get("table_continuation_source_ids") or []
            ],
        }
        if represented.isdisjoint(linked_ids):
            missing.append(source_id)
    return list(dict.fromkeys(missing))


def _amendment_target_links_base_evidence(
    item: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> bool:
    if (
        item.get("direct_field_match")
        or item.get("anchor_match")
    ):
        return True
    target_text = _table_binding_target_text(item)
    if not target_text:
        return False
    bases = [candidate for candidate in evidence if candidate.get("source_role") == "base_rfp"]
    target_pages = {
        int(value)
        for value in re.findall(
            r"\b(?:page|pg)\s*(?:no\.?\s*)?[:.\s-]*(\d{1,4})\b",
            target_text,
            re.I,
        )
    }
    if target_pages.intersection(
        int(candidate.get("page") or 0) for candidate in bases
    ):
        return True
    target_references = set(_reference_keys(target_text)) | set(
        _clause_locator_keys(target_text)
    )
    target_codes = {
        value.casefold()
        for value in re.findall(r"\b[A-Z]{1,8}\d+(?:\.\d+)*\b", target_text, re.I)
    }
    for candidate in bases:
        searchable = " ".join(
            [
                str(candidate.get("text") or ""),
                str(candidate.get("clause_number") or ""),
                " ".join(str(value) for value in candidate.get("section_path") or []),
            ]
        )
        if target_references.intersection(
            set(_reference_keys(searchable)) | set(_clause_locator_keys(searchable))
        ):
            return True
        base_codes = {
            value.casefold()
            for value in re.findall(r"\b[A-Z]{1,8}\d+(?:\.\d+)*\b", searchable, re.I)
        }
        if target_codes.intersection(base_codes):
            return True
    return False


def _automatic_amendment_visual_page_id(
    evidence: list[dict[str, Any]],
) -> str:
    candidates = [
        item
        for item in evidence
        if item.get("source_role") == "post_base_update"
        and item.get("effective_state") != "superseded"
        and item.get("page_id")
        and _is_layout_sensitive_evidence(item)
        and _binding_update_strength(item) > 0
    ]
    if not candidates:
        return ""
    best = max(
        candidates,
        key=lambda item: (
            _binding_update_strength(item),
            bool(item.get("amendment_candidate")),
            bool(item.get("direct_field_match") or item.get("anchor_match")),
            int(item.get("authority_rank") or 0),
            float(item.get("amendment_relevance_score") or 0),
        ),
    )
    return str(best.get("page_id") or "")


def _automatic_visual_page_id(
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
    result: dict[str, Any],
    validation_issues: list[str] | None = None,
) -> str:
    if result.get("status") not in {
        "found",
        "not_found",
        "ambiguous",
        "need_more_evidence",
    }:
        return ""

    current_ids = set(effective_state.get("current_source_ids") or [])
    excluded_ids = set(effective_state.get("superseded_source_ids") or []) | set(
        effective_state.get("historical_source_ids") or []
    )
    controlling_ids = set(effective_state.get("controlling_source_ids") or [])
    cited_ids = {
        str(item.get("source_id") or "")
        for item in result.get("evidence") or []
        if item.get("source_id")
    }
    candidates = [
        item
        for item in evidence
        if item.get("page_id")
        and item.get("source_id") not in excluded_ids
        and (not current_ids or item.get("source_id") in current_ids)
        and _is_layout_sensitive_evidence(item)
    ]
    candidates = [
        item
        for item in candidates
        if item.get("source_id") in controlling_ids
        or item.get("amendment_candidate")
        or item.get("direct_field_match")
        or item.get("anchor_match")
        or item.get("facet_match")
        or item.get("formal_revision")
        or item.get("formal_revision_scope")
    ]
    if not candidates:
        return ""
    if (
        not _requires_exhaustive_coverage(field)
        and result.get("status") == "found"
        and not validation_issues
        and not any(
            _binding_update_strength(item)
            or item.get("revision_layout")
            or item.get("table_continuation_source_ids")
            or item.get("formal_revision")
            for item in candidates
        )
    ):
        return ""

    cited_candidates = [
        item
        for item in evidence
        if item.get("page_id")
        and item.get("source_id") in cited_ids
        and item.get("source_id") not in excluded_ids
        and (not current_ids or item.get("source_id") in current_ids)
    ]
    cited_authority = max(
        (int(item.get("authority_rank") or 0) for item in cited_candidates),
        default=0,
    )
    newer_candidate = any(
        int(item.get("authority_rank") or 0) > cited_authority
        for item in candidates
    )
    if cited_candidates and not newer_candidate:
        return str(
            max(
                cited_candidates,
                key=lambda item: (
                    item.get("source_id") in controlling_ids,
                    bool(item.get("direct_field_match") or item.get("anchor_match")),
                    float(item.get("retrieval_score") or 0),
                ),
            ).get("page_id")
            or ""
        )

    best = max(
        candidates,
        key=lambda item: (
            _binding_update_strength(item),
            bool(
                item.get("formal_revision")
                or item.get("direct_field_match")
                or item.get("anchor_match")
                or item.get("facet_match")
            ),
            int(item.get("authority_rank") or 0),
            item.get("source_id") in cited_ids
            and item.get("source_id") in controlling_ids,
            item.get("source_id") in controlling_ids,
            bool(item.get("formal_revision")),
            bool(item.get("direct_field_match")),
            bool(item.get("anchor_match")),
            bool(item.get("facet_match")),
            item.get("source_id") in cited_ids,
            float(item.get("retrieval_score") or 0),
        ),
    )
    return str(best.get("page_id") or "")


def _link_amendment_target(
    field: RfpFieldSpec,
    target: str,
    previous_text: str,
    evidence: list[dict[str, Any]],
) -> tuple[list[str], str]:
    normalized_target = _phrase_text(target.replace("_", " "))
    configured_targets = {
        _phrase_text(field.key.replace("_", " ")),
        _phrase_text(field.label),
    }
    configured_target = bool(normalized_target and normalized_target in configured_targets)
    bases = [item for item in evidence if item.get("source_role") == "base_rfp"]
    query_terms = set(_tokens(f"{target} {previous_text}"))
    reference_keys = set(_reference_keys(f"{target} {previous_text}"))
    scored: list[tuple[int, int, str]] = []
    for item in bases:
        source_id = str(item.get("source_id") or "")
        searchable = " ".join(
            [
                str(item.get("text") or ""),
                str(item.get("clause_number") or ""),
                " ".join(str(part) for part in item.get("section_path") or []),
            ]
        )
        overlap = len(query_terms & set(_tokens(searchable)))
        shared_references = reference_keys & set(_reference_keys(searchable))
        score = overlap + 20 * len(shared_references)
        if score:
            scored.append((score, len(shared_references), source_id))
    scored.sort(reverse=True)
    best_score = scored[0][0] if scored else 0
    linked = [
        source_id
        for score, shared_reference_count, source_id in scored[:3]
        if shared_reference_count or score >= max(4, best_score - 2)
    ][:2]
    if linked:
        return linked, "linked"
    if configured_target:
        return [], "configured_field"
    if reference_keys or re.search(r"\b(?:section|clause|annexure|appendix)\b", target, re.I):
        return [], "explicit_reference"
    return [], "unresolved"


def _amendment_source_binds_field(
    field: RfpFieldSpec,
    item: dict[str, Any],
    retriever: Any,
) -> bool:
    if item.get("formal_revision") or _formal_revision_cells(item):
        return _formal_revision_binds_field(field, item, retriever)
    if _nonoperative_amendment_label(field, item):
        return False
    if _direct_scalar_update_value(field, item):
        return True
    response = _compact_text(
        item.get("issuer_response") or item.get("issuer_response_or_revised_text")
    )
    if _field_requires_operative_ownership(field):
        claim_text = f"{_unit_text(item)} {response}"
        if not _has_operative_ownership_claim(claim_text) and not (
            response
            and _pointer_only_amendment_response(response)
            and _clarification_target_matches_field(field, item)
        ):
            return False
    if response and (
        _issuer_response_matches_field_anchor(field, response)
        or _clarification_target_matches_field(field, item)
    ):
        return _field_subject_compatible(field, f"{_unit_text(item)} {response}")
    target_text = (
        _table_binding_target_text(item)
        if item.get("unit_type") in {"table_row", "table_header"}
        else _unit_text(item)
    )
    return bool(
        target_text
        and _binding_text_match(target_text, field)
        and _field_subject_compatible(field, target_text)
    )


def _nonoperative_amendment_label(
    field: RfpFieldSpec, item: dict[str, Any]
) -> bool:
    cells = [_phrase_text(cell) for cell in _table_cells(item)]
    header_cells = {
        "sl",
        "s no",
        "serial no",
        "category",
        "requirement",
        "compliance",
        "remarks",
        "response",
        "description",
        "particulars",
    }
    if item.get("unit_type") == "table_header" or (
        len(cells) >= 2 and all(cell in header_cells for cell in cells)
    ):
        return True

    target = _phrase_text(f"{field.key} {field.label} {field.description}")
    identity_field = any(
        cue in target
        for cue in (
            "tender id",
            "tender reference",
            "rfp reference",
            "reference number",
            "tender title",
            "tender type",
            "document type",
        )
    )
    if not identity_field or _unit_page(item) != 1:
        return False
    text = _phrase_text(_unit_text(item))
    return not any(
        cue in text
        for cue in (
            "revised",
            "changed",
            "amended",
            "modified",
            "updated",
            "shall read",
            "read as",
            "replace",
            "supersede",
        )
    )


def _field_requires_operative_ownership(field: RfpFieldSpec) -> bool:
    target = _phrase_text(
        " ".join(
            [
                field.key,
                field.label,
                field.description,
                field.prompt,
                *field.retrieval_guidance.aliases,
                *field.retrieval_guidance.anchor_phrases,
            ]
        )
    )
    return "ownership" in target or (
        "source code" in target
        and any(cue in target for cue in ("ipr", "intellectual property", "rights"))
    )


def _has_operative_ownership_claim(text: str) -> bool:
    normalized = _compact_text(text)
    return any(
        re.search(pattern, normalized, re.I)
        for pattern in (
            r"\b(?:shall|will|must)\s+(?:vest|own|retain|assign|transfer|licen[cs]e|"
            r"hand\s+over|deliver)\b",
            r"\b(?:vest(?:ed|s)?|owned|belong(?:s|ed)?|assigned|transferred|licensed)\s+"
            r"(?:in|to|by|with|under)\b",
            r"\b(?:ownership|title)\s+(?:in|of|to)\b.{0,100}\b"
            r"(?:vest|remain|belong|transfer|assign|retain)\b",
            r"\bsource\s+code\b.{0,100}\b(?:hand(?:ed)?\s+over|deliver(?:ed)?|"
            r"transfer(?:red)?|assign(?:ed)?|vest(?:ed)?|own(?:ed)?|licen[cs](?:e|ed)|"
            r"custody)\b",
            r"\b(?:source\s+code|intellectual\s+property(?:\s+rights)?|ipr|"
            r"third-party\s+software)\b.{0,100}\b(?:remain(?:s)?\s+with|"
            r"vest(?:s|ed)?\s+with|belong(?:s)?\s+to|licensed\s+under)\b",
        )
    )


def _issuer_response_matches_field_anchor(
    field: RfpFieldSpec,
    issuer_response: str,
) -> bool:
    return bool(
        _binding_text_match(issuer_response, field)
        and _field_subject_compatible(field, issuer_response)
    )


def _short_binding_field_response(
    field: RfpFieldSpec,
    item: dict[str, Any],
    issuer_response: str,
) -> bool:
    normalized = _phrase_text(issuer_response)
    if not normalized or len(_WORD_RE.findall(normalized)) > 24:
        return False
    if not any(
        cue in normalized
        for cue in (
            "to be submitted",
            "shall be submitted",
            "must be submitted",
            "is required",
            "shall apply",
            "is applicable",
            "is permitted",
            "not permitted",
            "not allowed",
            "agreed",
            "accepted",
        )
    ):
        return False
    return bool(
        _clarification_target_matches_field(field, item)
        or (
            item.get("unit_type") not in {"table_row", "table_header"}
            and _binding_text_match(str(item.get("text") or ""), field)
            and _field_subject_compatible(field, str(item.get("text") or ""))
        )
    )


def _clarification_target_matches_field(
    field: RfpFieldSpec,
    item: dict[str, Any],
) -> bool:
    cells = _table_cells(item)
    headers = [str(value) for value in item.get("table_headers") or []]
    request_index = _table_header_index(headers, "query", "request")
    response_index = _table_header_index(headers, "response", "clarification")
    if response_index is None or response_index <= 0 or not cells:
        return False
    if request_index is not None and 0 < request_index < len(cells):
        target_end = request_index
    elif (
        request_index is not None
        and response_index >= len(cells)
        and len(cells) >= 3
    ):
        target_end = len(cells) - 2
    else:
        target_end = min(response_index, len(cells) - 1)
    if target_end <= 0:
        return False
    target_text = " | ".join(cells[:target_end])
    return bool(
        _binding_text_match(target_text, field)
        and _field_subject_compatible(field, f"{target_text} {_unit_text(item)}")
    )


def _issuer_response_is_header(value: str) -> bool:
    return _phrase_text(value) in {
        "response",
        "issuer response",
        "bank response",
        "client response",
        "response of pre bid",
        "response of pre bid query",
        "evidence documents to be submitted",
        "minimum requirement criteria",
    }


def _pointer_only_amendment_response(issuer_response: str) -> bool:
    normalized = _phrase_text(issuer_response)
    return len(_tokens(issuer_response)) <= 12 and any(
        cue in normalized
        for cue in (
            "as per rfp",
            "as per rfe",
            "please refer corrigendum",
            "refer corrigendum",
            "please be guided by corrigendum",
            "guided by corrigendum",
            "please be guided by rfp",
            "please be guided by rfe",
            "not agreed",
        )
    )


def _amendment_state_source_ids(state: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *[str(item) for item in state.get("current_source_ids") or []],
                *[str(item) for item in state.get("historical_source_ids") or []],
                *[str(item) for item in state.get("target_source_ids") or []],
            ]
        )
    )
