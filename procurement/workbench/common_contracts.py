"""Evaluation-checklist normalization and reviewer-controlled changes."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from procurement.domain.models import new_id


_MAX_CONTRACT_CRITERIA = 2000
_MAX_GROUP_SIZE = 6


_MUTABLE_RULE_FIELDS = {
    "rule_family",
    "requirement",
    "mandatory",
    "failure_effect",
    "pass_rule",
    "required_documents",
}


def _group_source_ref(criterion: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = criterion.get("extensions")
    provenance = extensions.get("provenance") if isinstance(extensions, Mapping) else None
    candidates = provenance.get("source_references") if isinstance(provenance, Mapping) else None
    if not candidates:
        candidates = criterion.get("rfp_source_refs") or criterion.get("source_refs")
    return next((item for item in candidates or [] if isinstance(item, Mapping)), {})


def _group_anchor(reference: Mapping[str, Any]) -> tuple[str, str]:
    clause = str(reference.get("clause_number") or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+){2,}", clause):
        parent = clause.rsplit(".", 1)[0]
        return f"clause:{parent}", f"Clause {parent}"
    source_id = str(reference.get("source_id") or "").strip()
    table = re.match(r"^(.*?-t\d+)-row-", source_id)
    if table:
        return f"table:{table.group(1)}", "Table requirements"
    return f"source:{source_id or 'unknown'}", "Source requirement"


def _group_evidence(criterion: Mapping[str, Any]) -> tuple[str, ...]:
    family = str(criterion.get("family") or "other").casefold()
    if family == "technical":
        return ("technical offer / compliance schedule",)
    rule = criterion.get("rule")
    documents = rule.get("required_documents") if isinstance(rule, Mapping) else None
    normalized = sorted({str(item).strip().casefold() for item in documents or [] if str(item).strip()})
    return tuple(normalized or ["no bidder document"])


def _apply_evaluation_groups(draft: dict[str, Any]) -> dict[str, Any]:
    """Group only same-page, same-section checks while retaining every atomic criterion."""
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    labels: dict[tuple[Any, ...], tuple[str, str, Any, tuple[str, ...]]] = {}
    for criterion in draft.get("criteria") or []:
        if not isinstance(criterion, dict):
            continue
        reference = _group_source_ref(criterion)
        document_id = str(reference.get("document_id") or "").strip()
        page = reference.get("page")
        anchor, anchor_label = _group_anchor(reference)
        if not document_id or page in (None, ""):
            singleton = criterion.get("criterion_id") or hashlib.sha256(repr(criterion).encode("utf-8")).hexdigest()[:16]
            anchor = f"criterion:{singleton}"
        evidence = _group_evidence(criterion)
        review = criterion.get("review") if isinstance(criterion.get("review"), Mapping) else {}
        key = (
            document_id,
            page,
            anchor,
            str(criterion.get("family") or "other"),
            str(criterion.get("evaluation_scope") or "technical_qualification"),
            str(criterion.get("criterion_kind") or "obligation"),
            str(criterion.get("materiality") or "mandatory_blocking"),
            str(criterion.get("decision_effect") or (criterion.get("rule") or {}).get("failure_effect") or ""),
            bool((criterion.get("rule") or {}).get("mandatory", True)),
            review.get("included") is not False,
            evidence,
        )
        buckets.setdefault(key, []).append(criterion)
        labels[key] = (anchor_label, document_id, page, evidence)

    groups: list[dict[str, Any]] = []
    for key, criteria in buckets.items():
        chunks = [criteria[index:index + _MAX_GROUP_SIZE] for index in range(0, len(criteria), _MAX_GROUP_SIZE)]
        anchor_label, document_id, page, evidence = labels[key]
        for chunk_index, chunk in enumerate(chunks, start=1):
            digest = hashlib.sha256(repr((key, chunk_index)).encode("utf-8")).hexdigest()[:16]
            title = str(chunk[0].get("title") or "Evaluation requirement")
            if len(chunk) > 1:
                evidence_label = evidence[0] if len(evidence) == 1 else anchor_label
                title = f"{evidence_label.title()} — {anchor_label}"
                if len(chunks) > 1:
                    title += f" ({chunk_index} of {len(chunks)})"
            group = {
                "group_id": f"GROUP-{digest}",
                "title": title,
                "criterion_ids": [str(item.get("criterion_id") or "") for item in chunk],
                "source_document_id": document_id,
                "source_page": page,
                "evidence_set": list(evidence),
            }
            groups.append(group)
            for position, criterion in enumerate(chunk, start=1):
                extensions = criterion.get("extensions")
                extensions = copy.deepcopy(dict(extensions)) if isinstance(extensions, Mapping) else {}
                extensions["evaluation_group"] = {
                    "group_id": group["group_id"],
                    "title": title,
                    "position": position,
                    "size": len(chunk),
                    "source_document_id": document_id,
                    "source_page": page,
                    "evidence_set": list(evidence),
                }
                criterion["extensions"] = extensions
    draft["evaluation_groups"] = groups
    draft["grouping_version"] = "source-page-evidence.v1"
    return draft


def _text(value: Any, *, name: str, maximum: int = 8000, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise HTTPException(status_code=422, detail=f"{name} is required")
    if len(result) > maximum:
        raise HTTPException(status_code=422, detail=f"{name} exceeds {maximum} characters")
    return result


def _contract_review_defaults(criterion: Mapping[str, Any]) -> dict[str, Any]:
    extensions = criterion.get("extensions")
    bhel = extensions.get("bhel") if isinstance(extensions, Mapping) else None
    bhel = bhel if isinstance(bhel, Mapping) else {}
    evaluation = extensions.get("evaluation") if isinstance(extensions, Mapping) else None
    evaluation = evaluation if isinstance(evaluation, Mapping) else {}
    existing = criterion.get("review")
    existing = existing if isinstance(existing, Mapping) else {}
    origin = str(
        existing.get("origin")
        or bhel.get("origin")
        or ("bhel_baseline" if str(criterion.get("criterion_id") or "").startswith("BHEL-") else "tender_derived")
    )
    evaluation_scope = str(
        criterion.get("evaluation_scope")
        or evaluation.get("evaluation_scope")
        or bhel.get("evaluation_scope")
        or "technical_qualification"
    )
    applicability = str(
        criterion.get("applicability")
        or evaluation.get("applicability")
        or bhel.get("applicability")
        or "applicable"
    )
    default_included = bool(
        evaluation.get(
            "included_by_default",
            evaluation_scope == "technical_qualification" and applicability != "not_applicable",
        )
    )
    included = existing.get("included") if "included" in existing else default_included
    confirmed = bool(existing.get("confirmed")) and included
    return {
        "included": included,
        "origin": origin,
        "exclusion_reason": str(
            existing.get("exclusion_reason")
            or evaluation.get("default_exclusion_reason")
            or ("Outside the current technical-qualification scope." if not default_included else "")
        ),
        "change_reason": str(existing.get("change_reason") or ""),
        "confirmed": confirmed,
        "confirmed_by": str(existing.get("confirmed_by") or "") if confirmed else "",
        "confirmed_at": str(existing.get("confirmed_at") or "") if confirmed else "",
    }


def _normalize_contract_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    from procurement.evaluation.contracts import normalize_criterion

    normalized = copy.deepcopy(dict(draft))
    normalized["schema_version"] = "evaluation-checklist.v1"
    normalized["revision"] = int(normalized.get("revision") or 0)
    criteria = []
    for raw in normalized.get("criteria") or []:
        if not isinstance(raw, Mapping):
            continue
        criterion = copy.deepcopy(dict(raw))
        criterion["missing_fields"] = [
            "field:" + str(item).removeprefix("rule:FIELD-")
            if str(item).startswith("rule:FIELD-")
            else str(item)
            for item in criterion.get("missing_fields") or []
        ]
        extensions = criterion.get("extensions")
        bhel = extensions.get("bhel") if isinstance(extensions, Mapping) else None
        bhel = bhel if isinstance(bhel, Mapping) else {}
        evaluation = extensions.get("evaluation") if isinstance(extensions, Mapping) else None
        evaluation = evaluation if isinstance(evaluation, Mapping) else {}
        applicability = (
            criterion.get("applicability")
            or evaluation.get("applicability")
            or bhel.get("applicability")
            or "applicable"
        )
        criterion["applicability"] = (
            copy.deepcopy(dict(applicability))
            if isinstance(applicability, Mapping)
            else str(applicability)
        )
        criterion["evaluation_scope"] = str(
            criterion.get("evaluation_scope") or evaluation.get("evaluation_scope") or bhel.get("evaluation_scope") or "technical_qualification"
        )
        criterion["materiality"] = str(
            criterion.get("materiality")
            or evaluation.get("materiality")
            or bhel.get("materiality")
            or ("mandatory_blocking" if (criterion.get("rule") or {}).get("mandatory", True) else "advisory")
        )
        if not str(criterion.get("criterion_id") or "").strip():
            # Old stored review-only gap rows predate criterion identifiers.
            # Preserve them for display; they cannot be frozen or evaluated.
            criterion["review"] = _contract_review_defaults(criterion)
            criteria.append(criterion)
            continue
        # One semantic representation travels from review to freeze to evaluation;
        # existing checklist/UI keys remain intact for compatibility.
        criterion = normalize_criterion(criterion)
        canonical_applicability = criterion.get("applicability") or {}
        criterion["applicability"] = str(
            canonical_applicability.get("state")
            if isinstance(canonical_applicability, Mapping)
            else canonical_applicability
        )
        criterion["review"] = _contract_review_defaults(criterion)
        criteria.append(criterion)
    normalized["criteria"] = criteria
    normalized.setdefault("unautomated_requirements", [])
    normalized.setdefault("planning_notes", [])
    return _apply_evaluation_groups(normalized)


def _reviewed_contract_draft(
    current: Mapping[str, Any],
    submitted: Mapping[str, Any],
    *,
    reason: str,
    reviewed_by: str,
    reviewed_at: str,
) -> dict[str, Any]:
    """Apply the small, auditable set of reviewer-editable checklist fields."""
    canonical = _normalize_contract_draft(current)
    requested = submitted.get("criteria")
    if not isinstance(requested, list):
        raise HTTPException(status_code=422, detail="draft.criteria must be a list")
    if len(requested) > _MAX_CONTRACT_CRITERIA:
        raise HTTPException(status_code=422, detail="evaluation checklist is too large")

    current_gaps = list(canonical.get("unautomated_requirements") or [])
    requested_gaps = submitted.get("unautomated_requirements", current_gaps)
    if not isinstance(requested_gaps, list) or any(not isinstance(item, Mapping) for item in requested_gaps):
        raise HTTPException(status_code=422, detail="draft.unautomated_requirements must be a list")
    current_gap_keys = {
        json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
        for item in current_gaps
    }
    requested_gap_keys = [
        json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
        for item in requested_gaps
    ]
    if len(requested_gap_keys) != len(set(requested_gap_keys)) or not set(requested_gap_keys).issubset(current_gap_keys):
        raise HTTPException(status_code=422, detail="source-review passages may only be resolved, not added or changed")
    retained_gap_keys = set(requested_gap_keys)
    resolved_gaps = [
        item for item in current_gaps
        if json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str) not in retained_gap_keys
    ]
    resolved_gaps_by_id = {
        str(item.get("source_id") or item.get("criterion_id") or ""): item
        for item in resolved_gaps
        if str(item.get("source_id") or item.get("criterion_id") or "")
    }

    existing = {
        str(item.get("criterion_id") or ""): item
        for item in canonical.get("criteria") or []
        if str(item.get("criterion_id") or "")
    }
    seen: set[str] = set()
    reviewed: list[dict[str, Any]] = []
    for position, raw in enumerate(requested):
        if not isinstance(raw, Mapping):
            raise HTTPException(status_code=422, detail=f"criterion {position + 1} must be an object")
        incoming = dict(raw)
        incoming_id = str(incoming.get("criterion_id") or "").strip()
        base = existing.get(incoming_id)
        is_new = base is None
        if is_new and incoming_id and not incoming_id.startswith(("NEW-", "HUMAN-")):
            raise HTTPException(status_code=422, detail=f"unknown criterion_id: {incoming_id}")
        criterion_id = incoming_id if incoming_id.startswith("HUMAN-") else (f"HUMAN-{new_id()}" if is_new else incoming_id)
        if criterion_id in seen:
            raise HTTPException(status_code=422, detail=f"duplicate criterion_id: {criterion_id}")
        seen.add(criterion_id)

        if is_new:
            source_gap_id = str(incoming.get("source_gap_id") or "").strip()
            source_gap = resolved_gaps_by_id.get(source_gap_id)
            source_refs = copy.deepcopy(source_gap.get("source_references") or []) if source_gap else []
            if source_gap and not source_refs:
                source_refs = [{
                    "source_id": source_gap.get("source_id"),
                    "document_id": source_gap.get("document_id"),
                    "page": source_gap.get("page"),
                    "quote": source_gap.get("source_quote") or "",
                }]
            criterion: dict[str, Any] = {
                "criterion_id": criterion_id,
                "rfp_source_refs": source_refs,
                "missing_fields": [],
                "needs_human_input": False,
                "depends_on": [],
                "notes": "Added during human review of the evaluation checklist.",
                "extensions": {"review": {"source": "human_added", "source_gap_id": source_gap_id}},
            }
            original_review = {"origin": "human_added"}
        else:
            criterion = copy.deepcopy(base)
            original_review = _contract_review_defaults(base)

        title = _text(incoming.get("title"), name="criterion title", maximum=2000, required=True)
        family = _text(incoming.get("family"), name="criterion family", maximum=255, required=True)
        incoming_rule = incoming.get("rule")
        if not isinstance(incoming_rule, Mapping):
            raise HTTPException(status_code=422, detail=f"rule is required for {criterion_id}")
        old_rule = criterion.get("rule") if isinstance(criterion.get("rule"), Mapping) else {}
        rule = copy.deepcopy(dict(old_rule))
        for key in _MUTABLE_RULE_FIELDS:
            if key not in incoming_rule:
                continue
            value = incoming_rule[key]
            if key == "mandatory":
                rule[key] = bool(value)
            elif key == "required_documents":
                if not isinstance(value, list) or len(value) > 100:
                    raise HTTPException(status_code=422, detail=f"required_documents is invalid for {criterion_id}")
                rule[key] = [
                    _text(item, name="required document", maximum=1000, required=True)
                    for item in value
                ]
            else:
                rule[key] = _text(
                    value,
                    name=f"rule.{key}",
                    maximum=8000 if key in {"requirement", "pass_rule"} else 255,
                    required=key in {"requirement", "pass_rule", "failure_effect"},
                )
        rule["rule_family"] = family
        rule.setdefault("requirement", title)
        rule.setdefault("mandatory", True)
        rule.setdefault("failure_effect", "manual_review")
        rule.setdefault("pass_rule", "all_required_evidence_must_pass")
        rule.setdefault("required_documents", [])

        incoming_review = incoming.get("review")
        incoming_review = incoming_review if isinstance(incoming_review, Mapping) else {}
        included = incoming_review.get("included") is not False
        confirmed = included and incoming_review.get("confirmed") is True
        was_confirmed = bool(original_review.get("confirmed"))
        exclusion_reason = _text(
            incoming_review.get("exclusion_reason"),
            name="exclusion reason",
            maximum=4000,
            required=not included,
        )
        criterion.update(
            {
                "criterion_id": criterion_id,
                "title": title,
                "family": family,
                "rule": rule,
                "applicability": _text(
                    incoming.get("applicability") or criterion.get("applicability") or "applicable",
                    name="applicability",
                    maximum=255,
                    required=True,
                ),
                "evaluation_scope": _text(
                    incoming.get("evaluation_scope") or criterion.get("evaluation_scope") or "technical_qualification",
                    name="evaluation scope",
                    maximum=255,
                    required=True,
                ),
                "materiality": _text(
                    incoming.get("materiality") or criterion.get("materiality") or "mandatory_blocking",
                    name="materiality",
                    maximum=255,
                    required=True,
                ),
                "review": {
                    "included": included,
                    "origin": original_review.get("origin") or "tender_derived",
                    "exclusion_reason": exclusion_reason,
                    "change_reason": reason,
                    "confirmed": confirmed,
                    "confirmed_by": (
                        original_review.get("confirmed_by")
                        if confirmed and was_confirmed
                        else reviewed_by if confirmed else ""
                    ),
                    "confirmed_at": (
                        original_review.get("confirmed_at")
                        if confirmed and was_confirmed
                        else reviewed_at if confirmed else ""
                    ),
                },
            }
        )
        reviewed.append(criterion)

    omitted = sorted(set(existing) - {item for item in seen if item in existing})
    if omitted:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Existing checks cannot be deleted; exclude them with a reason.",
                "criterion_ids": omitted,
            },
        )
    result = copy.deepcopy(canonical)
    result["criteria"] = reviewed
    result["unautomated_requirements"] = copy.deepcopy(requested_gaps)
    result["revision"] = int(canonical.get("revision") or 0) + 1
    result["last_review"] = {
        "reason": reason,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
    }
    return _normalize_contract_draft(result)
