"""Prompt payloads and visual/proof follow-up helpers."""

from __future__ import annotations

import re
from typing import Any

from .field_core import (
    _CONDITION_CUES,
    _DOCUMENT_CUES,
    _WORD_RE,
    _clean_list,
    _compact_text,
    _formal_revision_cells,
    _is_layout_sensitive_evidence,
    _phrase_text,
    _requires_exhaustive_coverage,
)
from .field_results import (
    _compact_field_evidence_item,
    _serialized_collection_artifact,
)
from .schema import RfpFieldSpec, RfpSectionSpec, RfpUnderstandingSchema

def _field_payload(
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
    round_number: int,
    prior_response: dict[str, Any] | None,
    validation_errors: list[str],
) -> dict[str, Any]:
    exhaustive = _requires_exhaustive_coverage(field)
    current_ids = set(effective_state.get("current_source_ids") or [])
    superseded_ids = set(effective_state.get("superseded_source_ids") or [])
    controlling_ids = set(effective_state.get("controlling_source_ids") or [])
    historical_ids = set(effective_state.get("historical_source_ids") or [])
    formal_revisions = []
    for item in evidence:
        revision = _formal_revision_cells(item)
        if (
            item.get("formal_revision") is True
            and revision
            and item.get("source_id") not in historical_ids | superseded_ids
        ):
            formal_revisions.append(
                {
                    "source_id": item["source_id"],
                    "existing_text": revision[0],
                    "revised_text": revision[1],
                }
            )
    formal_revisions = formal_revisions[:8]
    current_evidence = sorted(
        (
            item
            for item in evidence
            if item.get("source_id") in current_ids
            and item.get("source_id") not in historical_ids
        ),
        key=lambda item: (
            item.get("source_id") in controlling_ids,
            bool(item.get("scalar_value_match")),
            bool(item.get("answer_shape_match")),
            bool(item.get("direct_field_match")),
            bool(item.get("anchor_match")),
            bool(item.get("likely_section_match")),
            bool(item.get("facet_match")),
            float(item.get("retrieval_score") or 0),
            int(item.get("authority_rank") or 0),
        ),
        reverse=True,
    )
    proof_repair = any("required_documents" in error for error in validation_errors)
    is_document_inventory = "mandatory bid document" in _phrase_text(
        f"{field.key} {field.label}"
    )
    if is_document_inventory:
        proof_bearing = [
            item for item in current_evidence
            if any(cue in str(item.get("text") or "").casefold() for cue in _DOCUMENT_CUES)
        ]
        current_evidence = [
            *proof_bearing,
            *[item for item in current_evidence if item not in proof_bearing],
        ]
    elif proof_repair:
        cited_ids = {
            str(item.get("source_id") or "")
            for item in (prior_response or {}).get("evidence") or []
            if item.get("source_id")
        }
        trusted_ids = controlling_ids | cited_ids
        current_evidence = [
            item
            for item in current_evidence
            if item.get("source_id") in trusted_ids
            and any(cue in str(item.get("text") or "").casefold() for cue in _DOCUMENT_CUES)
        ]
    evidence_limit = (
        24 if round_number <= 1 else 32
    ) if exhaustive else (
        16 if round_number <= 1 else 20
    )
    current_evidence = current_evidence[:evidence_limit]
    covered_facets = list(
        dict.fromkeys(
            str(facet)
            for item in current_evidence
            for facet in item.get("facet_matches") or []
            if facet
        )
    )
    visual_review_page_ids = list(
        dict.fromkeys(
            str(item.get("page_id") or "")
            for item in current_evidence
            if item.get("page_id")
            and _is_layout_sensitive_evidence(item)
        )
    )[:8]
    visual_required = bool(
        visual_review_page_ids
        and (
            exhaustive
            or validation_errors
            or any(
                item.get("formal_revision")
                or item.get("formal_revision_scope")
                or item.get("revision_layout")
                or item.get("table_continuation_source_ids")
                for item in current_evidence
            )
        )
    )
    superseded_evidence = [
        item for item in evidence if item.get("source_id") in superseded_ids
    ][:8]
    historical_evidence = [
        item for item in evidence if item.get("source_id") in historical_ids
    ][:8]
    return {
        "task": f"resolve_rfp_field_{field.key}",
        "schema_id": schema.schema_id,
        "section": {
            "key": section.key,
            "label": section.label,
            "prompt": section.prompt,
        },
        "field": field.model_dump(mode="json"),
        "schema_prompt": schema.prompt,
        "coverage_mode": "exhaustive" if exhaustive else "focused",
        "coverage_stats": {
            "retrieved_candidates": len(evidence),
            "supplied_current_evidence": len(current_evidence),
            "omitted_candidates": max(0, len(evidence) - len(current_evidence)),
            "covered_retrieval_facets": covered_facets,
        },
        "visual_review_page_ids": visual_review_page_ids,
        "visual_required": visual_required,
        "round": round_number,
        "rules": [
            "Resolve only this configured field. Ignore unrelated tender content.",
            "The evidence array is a field dossier: facet_matches records field-element alignment, while section_id, table, and table_continuation_source_ids record continuity. Read every aligned continuation before answering.",
            (
                "Exhaustive coverage mode: inspect every supplied snippet and synthesize every distinct current requirement, condition, exception, alternative, threshold, and proof for this field; never stop after the first matching clause."
                if exhaustive
                else "Focused coverage mode: return the complete direct value and every material qualifier supported by the supplied evidence."
            ),
            "Use coverage_stats as a completeness signal. If omitted candidates may contain another material clause, return need_more_evidence with modality more_text and a focused query before finalizing.",
            "When table alignment, continuation rows, OCR corruption, stamps, handwriting, or missing visual structure could change the answer, return need_more_evidence with modality table_image or page_image and the relevant supplied page_ids. Do not guess from flattened text.",
            "When visual_required is true and no page image is supplied in this call, request the single most relevant visual_review_page_id before finalizing. Otherwise request images only for a real layout ambiguity.",
            "Use only supplied source IDs, text, metadata, and page images.",
            "Every found answer must copy at least one exact supplied source_id or page_id into the evidence fields.",
            "A post_base_update source supersedes base_rfp text only when it explicitly changes that field.",
            "effective_state is application-compiled authority, not a suggestion. Resolve the answer from current_source_ids and typed operations.",
            "If effective_state.package_amendment_status is unresolved or unresolved_conflicts is non-empty, return ambiguous; never guess a current value.",
            "When controlling_source_ids is non-empty, those sources are the latest field-specific authority. They determine the current value or deletion; other current sources may add only compatible supporting conditions.",
            "Authority applies only to the same aligned element. A newer but unrelated row, table, or clause cannot supersede an older field element merely because it appears later in the package.",
            "When dedicated submission instructions conflict with an annexure, covering-letter, or form template, use the operative instructions for the value and preserve the template wording only as an explicit inconsistency or exception.",
            "Never use superseded_evidence for the current value, status, conditions, or required documents; it is supplied only to explain history.",
            "Never carry a value, permission, exception, or relaxation from historical_evidence into the current answer unless controlling evidence explicitly preserves it.",
            "A later replace_scope operation means the complete named section or table replaces older versions. An omitted prior row is no longer effective unless the replacement preserves it explicitly.",
            "A delete operation removes only its named target. In exhaustive coverage mode, preserve other current members stated by the controlling source and return not_found only when the entire configured field is deleted.",
            "When a latest complete replacement omits a prior criterion, return not_found for that criterion; do not encode the deletion as found with a value such as 'Not required'.",
            "formal_revisions contains pre-parsed existing-versus-revised cells. When relevant, revised_text controls and existing_text must not survive as the effective answer.",
            "Before answering, audit every supplied snippet for direct value, subject, mandatory versus preferred status, scope, thresholds, periods, conditions, exceptions, proof documents, conflicts, and amendment effects.",
            "Keep each output list item under 45 words, merge repeated qualifiers, and never duplicate the same clause across value, conditions, exceptions, and required_documents.",
            "Preserve every material condition, exception, relaxation, alternative, threshold, fallback, and required proof document; a headline value alone is incomplete.",
            "required_documents means named bid-stage proof for this field, including formats, annexures, certificates, declarations, undertakings, letters, purchase orders, and audited statements.",
            "For a scored technical evaluation, an explicitly named presentation, demonstration, architecture walkthrough, delivery plan, case study, test log, recorded demo, data sample, or test summary is field-specific required evidence when the bidder must present it to earn marks, even if the row does not say 'shall submit'.",
            "When validation_errors concerns required_documents, preserve the supported prior value, conditions, exceptions, and citations; repair the required_documents lane from the supplied proof-bearing evidence instead of rewriting the answer.",
            "A condition, exception, or required document must be directly linked to this field and supported by a cited source; do not import nearby tender facts merely because they appear in the evidence pack.",
            "For descriptive identity or methodology fields, leave required_documents empty unless the RFP explicitly mandates a field-specific submission.",
            "exceptions means exemptions, relaxations, alternatives, deletions, fallbacks, and unresolved contradictions; do not silently harmonize conflicting source text.",
            "In a clarification table, a bidder request is not binding unless the issuer response accepts it. 'As per RFP' preserves the base clause; 'refer corrigendum' requires the formal revised clause.",
            "Distinguish bidder eligibility from technical scoring, and bidder credentials from product, solution, OEM, or post-award requirements.",
            "Return source IDs and page IDs, not copied quotations; the application hydrates quotations locally.",
            "Use need_more_evidence when the supplied material cannot support a reliable answer.",
            "anchor_phrases normally rank retrieval evidence. Only when field.validation.require_anchor_for_positive is true must a found positive value cite anchor_match=true evidence.",
            "Use not_found only when the focused search is relevant but contains no statement of the field.",
            "Never infer a procurement convention that is not stated in the evidence.",
            "Template labels or blanks such as 'Name of Purchaser' are not values; request more evidence instead.",
            "The value field must contain only the direct normalized value, never conditions, evidence narration, explanatory prose, or repeated whitespace.",
            (
                "Hard output budget: keep an exhaustive narrative value under 180 words and use at most eight conditions, exceptions, or documents, each under 24 words. Synthesize compatible clauses without dropping distinct obligations."
                if exhaustive
                else "Hard output budget: keep a narrative value under 120 words. For a narrative string use at most four conditions, exceptions, or documents, each under 20 words. Synthesize compatible clauses instead of copying them."
            ),
            "Keep every list item concise and never emit newline padding.",
            "When requesting evidence, provide only a retrieval query of at most 12 words; do not explain the request.",
        ],
        "effective_state": effective_state,
        "evidence": [_compact_field_evidence_item(item) for item in current_evidence],
        "superseded_evidence": [
            _compact_field_evidence_item(item) for item in superseded_evidence
        ],
        "historical_evidence": [
            _compact_field_evidence_item(item) for item in historical_evidence
        ],
        "formal_revisions": formal_revisions,
        "prior_response": prior_response or {},
        "validation_errors": validation_errors,
    }


def _compact_projection_payload(
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    effective_state: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    target_source_ids = list(
        dict.fromkeys(
            str(value)
            for operation in effective_state.get("operations") or []
            for value in operation.get("target_source_ids") or []
        )
    )
    context_ids = list(
        dict.fromkeys(
            [
                *[str(value) for value in effective_state.get("controlling_source_ids") or []],
                *target_source_ids,
            ]
        )
    )
    context_by_id = {
        str(item.get("source_id") or ""): item
        for item in evidence
        if item.get("source_id")
    }
    return {
        "task": f"project_rfp_field_{field.key}",
        "schema_id": schema.schema_id,
        "section": {"key": section.key, "label": section.label},
        "field": {
            "key": field.key,
            "label": field.label,
            "description": field.description,
            "prompt": field.prompt,
            "value_type": field.value_type,
        },
        "rules": [
            "Project the already validated effective events into one current field answer; do not reinterpret chronology.",
            "Use only supplied source IDs and cite every event needed for the answer.",
            "Hard output limit: return no more than 700 tokens of JSON; combine repeated wording without dropping distinct facts.",
            "For an array field, emit one concise item per distinct current requirement and preserve every alternative or relaxation.",
            "Keep a narrative value under 120 words and every list item under 45 words; do not copy whole clauses.",
            "Preserve field-specific thresholds, periods, conditions, exceptions, scoring rules, and proof documents once each.",
            "source_context contains the controlling amendment rows and their linked base targets. Preserve every unaffected base condition and apply each event only to its named target.",
            "Every unchanged_condition_candidate must appear in conditions unless an effective event explicitly replaces or deletes that same condition.",
            "In clarification tables, bidder requests are non-binding; only the issuer response and validated events can change the base requirement.",
            "Parse proof artifacts from the effective row text when they appear after a table separator or in a document column.",
            "Exclude nearby content that does not directly change this configured field.",
            "Return ambiguous rather than inventing a missing relationship.",
        ],
        "effective_events": [
            {
                "operation": operation.get("operation"),
                "target": str(operation.get("target") or "")[:700],
                "previous_text": str(operation.get("previous_text") or "")[:1200],
                "effective_text": str(operation.get("effective_text") or "")[:2200],
                "required_documents": operation.get("required_documents") or [],
                "source_ids": operation.get("source_ids") or [],
                "target_source_ids": operation.get("target_source_ids") or [],
            }
            for operation in effective_state.get("operations") or []
        ],
        "source_context": [
            _compact_field_evidence_item(context_by_id[source_id])
            for source_id in context_ids[:8]
            if source_id in context_by_id
        ],
        "unchanged_condition_candidates": _context_condition_candidates(
            [context_by_id[source_id] for source_id in target_source_ids if source_id in context_by_id]
        ),
    }


def _context_condition_candidates(items: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for item in items:
        for segment in re.split(r"\s*\|\s*|(?<=[.;])\s+", str(item.get("text") or "")):
            compact = _compact_text(segment)
            if (
                4 <= len(_WORD_RE.findall(compact)) <= 80
                and any(cue in _phrase_text(compact) for cue in _CONDITION_CUES)
            ):
                candidates.append(compact[:600])
    return list(dict.fromkeys(candidates))[:8]


def _targeted_visual_verification_payload(
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
    prior_response: dict[str, Any],
    validation_errors: list[str],
    page_ids: list[str],
) -> dict[str, Any]:
    payload = _compact_projection_payload(
        schema,
        section,
        field,
        effective_state,
        evidence,
    )
    supplied_ids = {
        str(item.get("source_id") or "") for item in payload["source_context"]
    }
    page_context = [
        item
        for item in evidence
        if str(item.get("page_id") or "") in set(page_ids)
        and str(item.get("source_id") or "") not in supplied_ids
    ]
    payload["task"] = f"verify_rfp_field_visual_{field.key}"
    payload["rules"] = [
        "Return one complete corrected field answer, preserving every supported lane from prior_response.",
        "Use the supplied image only to resolve the listed validation_errors; do not reopen unrelated tender content.",
        "Apply effective_events atomically and preserve unaffected source_context conditions.",
        "Bidder requests are non-binding unless the issuer response accepts them.",
        "Keep the JSON under 700 tokens and cite only supplied source IDs or page IDs.",
    ]
    payload["visual_page_ids"] = list(dict.fromkeys(page_ids))[:2]
    payload["validation_errors"] = validation_errors[:6]
    payload["prior_response"] = prior_response
    payload["source_context"] = [
        *payload["source_context"],
        *[_compact_field_evidence_item(item) for item in page_context[:4]],
    ][:8]
    payload["unchanged_condition_candidates"] = list(
        dict.fromkeys(
            [
                *payload.get("unchanged_condition_candidates", []),
                *_context_condition_candidates(page_context),
            ]
        )
    )[:8]
    return payload


def _proof_excerpt(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    positions = sorted(
        {
            index
            for cue in _DOCUMENT_CUES
            for index in [lowered.find(cue)]
            if index >= 0
        }
    )
    if not positions:
        return text[:limit]
    excerpts: list[str] = []
    for position in positions:
        excerpts.append(text[max(0, position - 240) : position + 560])
    return " ... ".join(dict.fromkeys(excerpts))[:limit]


def _proof_repair_payload(
    schema: RfpUnderstandingSchema,
    section: RfpSectionSpec,
    field: RfpFieldSpec,
    evidence: list[dict[str, Any]],
    effective_state: dict[str, Any],
    prior_response: dict[str, Any],
    validation_errors: list[str],
) -> dict[str, Any]:
    selected = _field_payload(
        schema,
        section,
        field,
        evidence,
        effective_state,
        2,
        prior_response,
        validation_errors,
    )["evidence"]
    compact_evidence: list[dict[str, Any]] = []
    remaining_chars = 10_000
    for item in selected[:8]:
        text = _proof_excerpt(str(item.get("text") or ""), min(1800, remaining_chars))
        if not text:
            continue
        compact_evidence.append(
            {
                key: item.get(key)
                for key in (
                    "source_id",
                    "document_id",
                    "page",
                    "page_id",
                    "section_path",
                    "source_role",
                    "revision_sequence",
                    "authority_rank",
                )
                if item.get(key) not in (None, "", [])
            }
            | {"text": text}
        )
        remaining_chars -= len(text)
        if remaining_chars <= 0:
            break
    return {
        "task": f"repair_rfp_field_proof_{field.key}",
        "schema_id": schema.schema_id,
        "field": {
            "key": field.key,
            "label": field.label,
            "description": field.description,
            "prompt": field.prompt,
        },
        "accepted_answer": {
            key: prior_response.get(key)
            for key in ("value", "conditions", "exceptions", "required_documents")
        },
        "validation_errors": validation_errors,
        "rules": [
            "The accepted answer is already settled. Return only the complete required_documents lane and its source IDs; do not rewrite the answer.",
            "Include every explicitly named bid-stage proof or scored evaluation artifact directly supporting the accepted answer.",
            "Scored presentations, demonstrations, architecture walkthroughs, case studies, prior-assignment evidence, logs, recorded demos, data samples, and test summaries count as artifacts even without the words 'shall submit'.",
            "Do not list legal rights, ownership categories, capabilities, criteria labels, thresholds, or evaluation topics as documents unless the source requires the bidder to submit, present, furnish, hand over, or make available that artifact.",
            "Group components of one handover into a single artifact family; do not list plans, drawings, specifications, reports, or software as separate documents.",
            "When the accepted answer relies on third-party software rights governed by an EULA or licence, include the applicable licence/EULA as one artifact family.",
            "Do not add a conventional document that is absent from the supplied evidence.",
            "Copy only supplied source_id and page_id values.",
        ],
        "controlling_operations": [
            {
                key: operation.get(key)
                for key in (
                    "operation",
                    "target",
                    "effective_text",
                    "required_documents",
                    "source_ids",
                )
                if operation.get(key) not in (None, "", [])
            }
            for operation in (effective_state.get("operations") or [])[:4]
        ],
        "evidence": compact_evidence,
    }


def _merge_proof_repair(
    prior_response: dict[str, Any],
    repair: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(prior_response)
    merged["required_documents"] = _clean_list(
        repair.get("required_documents"), limit=16
    )
    merged["evidence_source_ids"] = list(
        dict.fromkeys(
            [
                *_clean_list(prior_response.get("evidence_source_ids")),
                *_clean_list(repair.get("evidence_source_ids")),
            ]
        )
    )
    merged["evidence_page_ids"] = list(
        dict.fromkeys(
            [
                *_clean_list(prior_response.get("evidence_page_ids")),
                *_clean_list(repair.get("evidence_page_ids")),
            ]
        )
    )
    if isinstance(repair.get("confidence"), (int, float)):
        merged["confidence"] = min(
            float(prior_response.get("confidence") or 0.0),
            float(repair["confidence"]),
        )
    merged.pop("evidence_request", None)
    return merged


def _merge_visual_verification(
    prior_response: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    prior_sources = set(_clean_list(prior_response.get("evidence_source_ids")))
    verification_sources = set(_clean_list(verification.get("evidence_source_ids")))
    if (
        prior_response.get("status") == "found"
        and verification.get("status") in {"not_found", "need_more_evidence"}
        and prior_sources
        and not prior_sources & verification_sources
    ):
        # The image did not inspect the evidence supporting the accepted value.
        return dict(prior_response)
    merged = dict(verification)
    for key in ("conditions", "exceptions", "required_documents"):
        merged[key] = [
            value
            for value in _clean_list(verification.get(key), limit=16)
            if not _serialized_collection_artifact(value)
        ]
    for key in ("evidence_source_ids", "evidence_page_ids"):
        merged[key] = _clean_list(verification.get(key))
    merged.pop("evidence_request", None)
    return merged



__all__ = [name for name in globals() if not name.startswith("__")]
