"""Completeness, grounding, and compact-evidence validation helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from .field_core import (
    _CONDITION_CUES,
    _DOCUMENT_CUES,
    _EXCEPTION_CUES,
    _PROOF_QUERY,
    _UPDATE_QUERY,
    _compact_text,
    _formal_revision_cells,
    _has_ocr_near_token,
    _needs_proof_lane,
    _phrase_text,
    _tokens,
)
from .field_results_evidence import _has_explicit_labeled_value
from .field_results_projection import _explicit_total_mark_values
from .schema import RfpFieldSpec


def _relevant_evidence_text(
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    cited = {
        str(item.get("source_id") or "")
        for item in result.get("evidence") or []
        if item.get("source_id")
    }
    selected = [item for item in evidence if item.get("source_id") in cited]
    if not selected:
        selected = evidence[:4]
    return " ".join(str(item.get("text") or "") for item in selected)


def _completeness_issues(
    field: RfpFieldSpec,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any] | None = None,
) -> list[str]:
    if result.get("status") == "not_found":
        return (
            ["not_found conflicts with an explicit labeled value in current base-RFP evidence"]
            if _has_explicit_labeled_value(field, evidence)
            else []
        )
    if result.get("status") != "found":
        return []
    evidence_text = _relevant_evidence_text(result, evidence)
    all_evidence_text = " ".join(str(item.get("text") or "") for item in evidence)
    text = evidence_text.casefold()
    issues: list[str] = []
    state = effective_state or {}
    controlling_ids = set(state.get("controlling_source_ids") or [])
    controlling_text = " ".join(
        str(item.get("text") or "")
        for item in evidence
        if item.get("source_id") in controlling_ids
    )
    current_ids_for_totals = set(state.get("current_source_ids") or [])
    current_text_for_totals = " ".join(
        str(item.get("text") or "")
        for item in evidence
        if not current_ids_for_totals or item.get("source_id") in current_ids_for_totals
    )
    controlling_max_marks = _explicit_total_mark_values(
        " ".join(part for part in (controlling_text, current_text_for_totals) if part)
    )
    answer_text = json.dumps(
        {
            "value": result.get("value"),
            "conditions": result.get("conditions") or [],
        },
        ensure_ascii=True,
    )
    multi_event_prompt = _phrase_text(field.prompt)
    if field.value_type == "date" and any(
        cue in multi_event_prompt
        for cue in ("every distinct", "do not discard")
    ):
        current_ids_for_events = set(state.get("current_source_ids") or [])
        current_event_text = _phrase_text(
            " ".join(
                str(item.get("text") or "")
                for item in evidence
                if not current_ids_for_events
                or item.get("source_id") in current_ids_for_events
            )
        )
        answer_event_text = _phrase_text(answer_text)
        event_labels = {
            label
            for label in (
                "qualifying",
                "eligibility",
                "technical",
                "commercial",
                "financial",
            )
            if label in current_event_text
        }
        missing_event_labels = sorted(
            label for label in event_labels if label not in answer_event_text
        )
        if missing_event_labels:
            issues.append(
                "value omits distinct dated events required by the field prompt: "
                + ", ".join(missing_event_labels)
            )
    answer_marks = set(re.findall(r"\b(\d+(?:\.\d+)?)\s*marks?\b", answer_text, re.I))
    answer_marks.update(
        re.findall(
            r"\bmax(?:imum)?\s*(?:marks?|score)?\s*[:\-]?\s*(\d+(?:\.\d+)?)",
            answer_text,
            re.I,
        )
    )
    missing_totals = sorted(controlling_max_marks - answer_marks)
    if missing_totals:
        issues.append(
            "value omits controlling maximum marks: " + ", ".join(missing_totals)
        )
    if state.get("unresolved_conflicts"):
        issues.append("the package amendment compiler has an unresolved controlling conflict")
    value_text = _phrase_text(str(result.get("value") or ""))
    if (
        not result.get("conditions")
        and not (field.value_type == "boolean" and result.get("value") is False)
        and not (
            field.value_type == "string"
            and len(_tokens(str(result.get("value") or ""))) >= 20
        )
        and any(cue in text for cue in _CONDITION_CUES)
        and not any(cue in value_text for cue in _CONDITION_CUES)
    ):
        issues.append("conditions are empty although cited evidence contains threshold or dependency language")
    if (
        field.value_type == "string_array"
        and len(result.get("value") or []) <= 1
        and any(cue in text for cue in ("one of the following", "any one of the following"))
        and " or " in f" {text} "
    ):
        issues.append("value omits alternatives from an explicit one-of-the-following criterion")
    answer_qualifier_text = _phrase_text(answer_text)
    if (
        not result.get("exceptions")
        and any(cue in text for cue in _EXCEPTION_CUES)
        and not any(cue in answer_qualifier_text for cue in _EXCEPTION_CUES)
        and not any(
            operation.get("operation") == "delete"
            for operation in state.get("operations") or []
        )
    ):
        issues.append("exceptions are empty although cited evidence contains exemption, fallback, or amendment language")
    if (
        _needs_proof_lane(field)
        and not result.get("required_documents")
        and any(cue in text for cue in _DOCUMENT_CUES)
    ):
        issues.append("required_documents are empty although cited evidence names bid proof artifacts")
    support_text = " ".join(
        part for part in (controlling_text, evidence_text, all_evidence_text) if part
    )
    support_token_list = _tokens(support_text)
    document_support_tokens = set(support_token_list)
    document_support_tokens.update(
        first + second
        for first, second in zip(support_token_list, support_token_list[1:])
        if len(first) >= 2 and len(second) >= 2 and len(first) + len(second) <= 24
    )
    unsupported_documents = []
    compact_support = re.sub(r"[^a-z0-9]+", "", support_text.casefold())
    for document in result.get("required_documents") or []:
        document_tokens = set(_tokens(document))
        compact_document = re.sub(r"[^a-z0-9]+", "", str(document).casefold())
        if compact_document and compact_document in compact_support:
            continue
        required_overlap = 1 if len(document_tokens) <= 1 else 2
        supported_count = sum(
            token in document_support_tokens
            or _has_ocr_near_token(token, document_support_tokens)
            for token in document_tokens
        )
        if supported_count < required_overlap:
            unsupported_documents.append(document)
    if unsupported_documents:
        issues.append(
            "required_documents contain items unsupported by cited evidence: "
            + ", ".join(unsupported_documents[:5])
        )
    cited_ids = {
        str(item.get("source_id") or "")
        for item in result.get("evidence") or []
        if item.get("source_id")
    }
    current_ids = set(state.get("current_source_ids") or [])
    superseded_ids = set(state.get("superseded_source_ids") or [])
    controlling_ids = set(state.get("controlling_source_ids") or [])
    if controlling_ids and cited_ids and not cited_ids & controlling_ids:
        issues.append("answer ignores the highest-authority field evidence")
    if cited_ids and cited_ids & superseded_ids and not cited_ids & current_ids:
        issues.append("answer relies only on superseded evidence")
    value_tokens = set(_tokens(json.dumps(result.get("value"), ensure_ascii=True)))
    superseded_tokens: set[str] = set()
    for item in evidence:
        if item.get("source_id") not in cited_ids or item.get("formal_revision") is not True:
            continue
        revision = _formal_revision_cells(item)
        if not revision:
            continue
        existing_tokens = set(_tokens(revision[0]))
        revised_tokens = set(_tokens(revision[1])) | set(_tokens(controlling_text))
        superseded_tokens.update(
            token
            for token in (existing_tokens - revised_tokens) & value_tokens
            if len(token) >= 6
            and not _has_ocr_near_token(token, revised_tokens)
        )
    if superseded_tokens:
        issues.append(
            "value repeats tokens present only in superseded existing_text: "
            + ", ".join(sorted(superseded_tokens)[:8])
        )
    current_text_tokens = {
        token
        for item in evidence
        if item.get("source_id") in current_ids
        for token in _tokens(str(item.get("text") or ""))
    }
    superseded_text_tokens = {
        token
        for item in evidence
        if item.get("source_id") in superseded_ids
        for token in _tokens(str(item.get("text") or ""))
    }
    stale_value_tokens = {
        token
        for token in value_tokens & superseded_text_tokens - current_text_tokens
        if len(token) >= 4
        and token
        not in {"bidd", "bidder", "tender", "proposal", "technical", "financial", "rfp"}
    }
    if stale_value_tokens:
        issues.append(
            "value contains terms supported only by superseded evidence: "
            + ", ".join(sorted(stale_value_tokens)[:8])
        )
    return issues


_BLOCKING_GROUNDING_PREFIXES = (
    "not_found conflicts with an explicit labeled value",
    "the package amendment compiler has an unresolved controlling conflict",
    "answer ignores the highest-authority field evidence",
    "answer relies only on superseded evidence",
    "required_documents contain items unsupported by cited evidence",
    "value repeats tokens present only in superseded existing_text",
    "value contains terms supported only by superseded evidence",
)


def _blocking_grounding_issues(issues: list[str]) -> list[str]:
    return [
        issue
        for issue in issues
        if any(issue.startswith(prefix) for prefix in _BLOCKING_GROUNDING_PREFIXES)
    ]


def _completeness_query(issues: list[str], field: RfpFieldSpec) -> str:
    joined = " ".join(issues).casefold()
    if "negative-evidence" in joined:
        return "explicit stated value amount date percentage formula applicable required"
    if "required_documents" in joined:
        field_terms = " ".join(
            [field.label, *field.retrieval_guidance.aliases[:6]]
        )
        return f"{field_terms} {_PROOF_QUERY}"
    if "exception" in joined or "amendment" in joined:
        return _UPDATE_QUERY
    return "field conditions exceptions scope supporting proof"


def _compact_field_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: item.get(key)
        for key in (
            "source_id",
            "document_id",
            "page",
            "page_id",
            "unit_type",
            "section_id",
            "table",
            "clause_number",
            "source_role",
            "source_file",
            "revision_sequence",
            "authority_rank",
            "effective_state",
            "formal_revision",
            "anchor_match",
            "likely_section_match",
            "answer_shape_match",
            "scalar_value_match",
            "direct_field_match",
            "facet_match",
        )
        if item.get(key) not in (None, "", [], False)
    }
    section_path = [
        _compact_text(value)[:180]
        for value in (item.get("section_path") or [])[-4:]
        if _compact_text(value)
    ]
    table_headers = [
        _compact_text(value)[:120]
        for value in (item.get("table_headers") or [])[:8]
        if _compact_text(value)
    ]
    if section_path:
        compact["section_path"] = section_path
    if table_headers:
        compact["table_headers"] = table_headers
    facet_matches = [
        _compact_text(value)[:120]
        for value in (item.get("facet_matches") or [])[:8]
        if _compact_text(value)
    ]
    if facet_matches:
        compact["facet_matches"] = facet_matches
    continuation_ids = [
        str(value)
        for value in (item.get("table_continuation_source_ids") or [])[:12]
        if value
    ]
    if continuation_ids:
        compact["table_continuation_source_ids"] = continuation_ids
    continuation_text = _compact_text(item.get("table_continuation_text"))[:1800]
    if continuation_text:
        compact["table_continuation_text"] = continuation_text
    compact["text"] = _compact_text(item.get("text"))[:2500]
    return compact


__all__ = [
    "_relevant_evidence_text",
    "_completeness_issues",
    "_BLOCKING_GROUNDING_PREFIXES",
    "_blocking_grounding_issues",
    "_completeness_query",
    "_compact_field_evidence_item",
]
