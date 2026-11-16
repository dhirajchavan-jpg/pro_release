"""BHEL bidder result normalization, review overlays, and comparisons."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterable
from collections.abc import Mapping

from .bhel_evaluation_common import (
    _BLOCKING,
    _FINDING_LIST_KEYS,
    _ID_FIELDS,
    _mapping,
    _mapping_list,
    _model_data,
    RESULT_STATUSES,
)
from .bhel_evaluation_matrix import _bhel_extension, _id_aliases, merge_bhel_profile
from .bhel_evaluation_profile import _evaluation_scope, load_bhel_profile
from .bhel_evaluation_sources import _explicit_sources, _normalize_source_references


def _finding_records(value: Any) -> list[dict[str, Any]]:
    value = _model_data(value)
    if isinstance(value, list):
        return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for key in _FINDING_LIST_KEYS:
        if key in value:
            nested = _finding_records(value[key])
            if nested:
                return nested
    if any(key in value for key in ("status", "result", "decision", "outcome", "answer")):
        return [deepcopy(dict(value))]
    records: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            record = deepcopy(dict(item))
            record.setdefault("criterion_id", str(key))
            records.append(record)
    return records


def _finding_id(finding: Mapping[str, Any]) -> str:
    for key in _ID_FIELDS:
        value = finding.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def _finding_index(findings: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for finding in _finding_records(findings):
        identifier = _finding_id(finding)
        for alias in _id_aliases(identifier):
            index.setdefault(alias, finding)
    return index


def _normalized_result(value: Any) -> str | None:
    if isinstance(value, bool):
        return "met" if value else "not_met"
    if isinstance(value, Mapping):
        for key in ("result", "status", "decision", "outcome", "answer"):
            result = _normalized_result(value.get(key))
            if result:
                return result
        return None
    if value is None:
        return None
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "met": "met",
        "pass": "met",
        "passed": "met",
        "yes": "met",
        "true": "met",
        "qualified": "met",
        "compliant": "met",
        "not_met": "not_met",
        "fail": "not_met",
        "failed": "not_met",
        "no": "not_met",
        "false": "not_met",
        "rejected": "not_met",
        "missing": "insufficient_evidence",
        "not_found": "insufficient_evidence",
        "insufficient": "insufficient_evidence",
        "insufficient_evidence": "insufficient_evidence",
        "absent": "insufficient_evidence",
        "review": "needs_review",
        "needs_review": "needs_review",
        "manual_review": "needs_review",
        "review_required": "needs_review",
        "unresolved": "needs_review",
        "unclear": "needs_review",
        "pending": "needs_review",
        "not_applicable": "not_applicable",
        "n_a": "not_applicable",
        "na": "not_applicable",
        "not_evaluated": "not_evaluated",
        "not_evaluated_yet": "not_evaluated",
        "out_of_scope": "not_evaluated",
    }
    return aliases.get(normalized)


def _finding_value(finding: Mapping[str, Any]) -> Any:
    for key in ("value", "extracted_value", "answer", "response"):
        if key in finding:
            return deepcopy(finding[key])
    return None


def _finding_reason(finding: Mapping[str, Any]) -> str:
    for key in ("reason", "rationale", "explanation", "why", "notes", "finding"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _finding_evidence(finding: Mapping[str, Any]) -> Any:
    for key in (
        "bidder_evidence",
        "evidence",
        "citations",
        "evidence_pages",
        "document_refs",
        "supporting_evidence",
    ):
        value = finding.get(key)
        if value not in (None, "", [], {}):
            return deepcopy(value)
    return []


def _has_evidence(finding: Mapping[str, Any]) -> bool:
    return _finding_evidence(finding) not in (None, "", [], {})


def _candidate_finding(index: Mapping[str, dict[str, Any]], check: Mapping[str, Any]) -> dict[str, Any] | None:
    identifiers = [
        str(check.get(key))
        for key in ("check_id", "criterion_id", "id", "label", "title")
        if check.get(key) not in (None, "")
    ]
    for identifier in identifiers:
        for alias in _id_aliases(identifier):
            if alias in index:
                return index[alias]
    return None


def _check_record(check: Mapping[str, Any], finding: Mapping[str, Any] | None, *, internal: bool = False) -> dict[str, Any]:
    scope = _evaluation_scope(check)
    bidder_check = not internal and scope == "technical_qualification"
    active = check.get("applicability") == "applicable"
    source_references = _normalize_source_references(check.get("source_references") or [])
    source_grounded = bool(source_references)
    if not active or not bidder_check and not internal:
        result = "not_applicable"
    elif finding is None:
        result = "not_evaluated" if internal else "insufficient_evidence"
    else:
        result = _normalized_result(finding) or "needs_review"
        required_proof = finding.get("required_proof") or check.get("required_proof") or []
        if (
            result == "met"
            and bidder_check
            and check.get("materiality") in _BLOCKING
            and required_proof
            and not _has_evidence(finding)
        ):
            result = "insufficient_evidence"
    if active and bidder_check and not source_grounded:
        # A bidder result cannot cure a tender requirement that has no auditable
        # tender citation. Preserve the criterion and require human review.
        result = "needs_review"
    evidence = _finding_evidence(finding or {})
    required = deepcopy((finding or {}).get("required_proof") or check.get("required_proof") or [])
    return {
        "check_id": check.get("check_id") or check.get("criterion_id"),
        "criterion_id": check.get("criterion_id") or check.get("check_id"),
        "label": check.get("label") or check.get("title") or check.get("check_id"),
        "requirement": check.get("requirement") or check.get("description") or check.get("label") or check.get("check_id"),
        "category": check.get("category") or "tender_derived",
        "origin": check.get("origin") or "bhel_baseline",
        "evaluation_scope": scope,
        "applicability": check.get("applicability") or "not_applicable",
        "materiality": check.get("materiality") or "mandatory_blocking",
        "result": result,
        "value": _finding_value(finding or {}),
        "required_proof": required,
        "bidder_evidence": evidence if not internal else [],
        "source_references": source_references,
        "source_grounded": source_grounded,
        "provenance_status": "verified" if source_grounded else ("missing" if active else "not_applicable"),
        "reason": _finding_reason(finding or {}) or (
            "The source does not activate this check." if result == "not_applicable" else ""
        ),
        "evidence_role": "internal_control" if internal else (
            "bidder_evidence" if bidder_check else "commercial_out_of_scope"
        ),
        "blocks_qualification": bool(
            bidder_check
            and check.get("materiality") in _BLOCKING
            and result in {"not_met", "insufficient_evidence", "needs_review", "not_evaluated"}
        ),
    }


def apply_bhel_human_reviews(
    extension: dict[str, Any],
    reviews: Iterable[Any],
    *,
    summary_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Overlay latest immutable human decisions on a copied BHEL result."""
    result = deepcopy(_model_data(extension)) if isinstance(_model_data(extension), dict) else {}
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        payload = getattr(review, "payload_json", None)
        if not isinstance(payload, Mapping) and isinstance(review, Mapping):
            payload = review.get("payload_json") or review
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        identifier = str(payload.get("criterion_id") or payload.get("checkpoint_id") or "").strip()
        if not identifier:
            continue
        action = getattr(review, "action", None)
        reason = getattr(review, "reason", None)
        created_at = getattr(review, "created_at", None)
        if isinstance(review, Mapping):
            action = action or review.get("action") or review.get("decision")
            reason = reason or review.get("reason")
            created_at = created_at or review.get("created_at")
        latest[identifier] = {
            **payload,
            "decision": str(action or payload.get("decision") or ""),
            "reason": str(reason or payload.get("reason") or ""),
            "recorded_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        }

    checks = _mapping_list(result.get("checks"))
    for check in checks:
        review = next(
            (value for key, value in latest.items() if _id_aliases(key) & _id_aliases(check.get("check_id") or check.get("criterion_id"))),
            None,
        )
        if review is None:
            continue
        check["ai_result"] = check.get("ai_result") or check.get("result")
        check["human_review"] = review
        decision = review.get("decision")
        if decision == "meets_requirement":
            check["result"] = "met"
        elif decision == "does_not_meet":
            check["result"] = "not_met"
        else:
            check["result"] = "needs_review"
        if review.get("corrected_value") not in (None, ""):
            check["value"] = deepcopy(review["corrected_value"])
        check["blocks_qualification"] = bool(
            check.get("materiality") in _BLOCKING
            and check.get("result") in {"not_met", "insufficient_evidence", "needs_review", "not_evaluated"}
        )
    result["checks"] = checks
    result["bidder_summary"] = (summary_fn or _summary)(result["checks"])
    return result


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in RESULT_STATUSES}
    blocking: list[str] = []
    for check in checks:
        result = check.get("result") if check.get("result") in counts else "needs_review"
        counts[result] += 1
        if check.get("blocks_qualification"):
            blocking.append(str(check.get("check_id") or check.get("criterion_id")))
    active = [check for check in checks if check.get("result") != "not_applicable"]
    if not active or all(check.get("result") == "not_evaluated" for check in active):
        qualification = "not_evaluated"
    elif any(check.get("result") == "not_met" and check.get("blocks_qualification") for check in checks):
        qualification = "not_qualified"
    elif any(
        check.get("result") in {"insufficient_evidence", "needs_review", "not_evaluated"}
        and check.get("blocks_qualification")
        for check in checks
    ):
        qualification = "review_required"
    else:
        qualification = "qualified"
    return {
        "qualification": qualification,
        "counts": counts,
        "blocking_check_ids": blocking,
        "evaluated_check_count": sum(counts[result] for result in ("met", "not_met")),
        "active_check_count": len(active),
    }


def build_bhel_extension(
    matrix: dict,
    findings: Any,
    report: dict,
    *,
    load_profile: Callable[[], dict | None] = load_bhel_profile,
    merge_profile: Callable[[dict, dict, dict], dict] = merge_bhel_profile,
    summary_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict:
    """Normalize one bidder report into a BHEL-specific, auditable extension."""
    profile = load_profile()
    if profile is None:
        return {}
    matrix_data = _model_data(matrix)
    findings_data = _model_data(findings)
    report_data = _model_data(report)
    merged = matrix_data if _bhel_extension(_mapping(matrix_data)) else merge_profile(matrix_data, {}, {})
    bhel = _bhel_extension(_mapping(merged))
    if not bhel:
        return {}
    index = _finding_index(findings_data if findings_data not in (None, {}) else report_data)

    bidder_checks = [
        check
        for check in [
            *_mapping_list(bhel.get("bidder_checks")),
            *_mapping_list(bhel.get("dynamic_checks")),
        ]
        if _evaluation_scope(check) == "technical_qualification"
    ]
    check_records = [
        _check_record(check, _candidate_finding(index, check))
        for check in bidder_checks
    ]
    controls = [
        _check_record(control, _candidate_finding(index, control), internal=True)
        for control in _mapping_list(bhel.get("internal_controls"))
    ]
    commercial_out_of_scope = []
    for commercial in _mapping_list(bhel.get("commercial_out_of_scope")):
        commercial["auditable_only"] = True
        commercial["blocks_qualification"] = False
        commercial["result"] = (
            "not_evaluated"
            if commercial.get("applicability") == "applicable"
            else "not_applicable"
        )
        commercial_out_of_scope.append(commercial)

    known_ids: set[str] = set()
    for check in [
        *bidder_checks,
        *_mapping_list(bhel.get("internal_controls")),
        *commercial_out_of_scope,
    ]:
        known_ids.update(_id_aliases(check.get("check_id")))
    for finding in _finding_records(findings):
        identifier = _finding_id(finding)
        if not identifier or _id_aliases(identifier) & known_ids:
            continue
        scope = _evaluation_scope(finding) or "commercial_out_of_scope"
        source_references = _explicit_sources(finding)
        grounded = bool(source_references)
        dynamic = {
            "check_id": identifier,
            "criterion_id": identifier,
            "label": finding.get("title") or finding.get("label") or identifier,
            "requirement": finding.get("requirement") or finding.get("description") or identifier,
            "category": finding.get("category") or "tender_derived",
            "kind": "bidder_check" if scope == "technical_qualification" else scope,
            "evaluation_scope": scope,
            "origin": "tender_derived",
            "materiality": finding.get("materiality") or "mandatory_blocking",
            "applicability": "applicable" if grounded else "not_applicable",
            "source_references": source_references,
        }
        if scope == "technical_qualification":
            check_records.append(_check_record(dynamic, finding))
        elif scope == "internal_control":
            controls.append(_check_record(dynamic, finding, internal=True))
        else:
            dynamic["auditable_only"] = True
            dynamic["blocks_qualification"] = False
            commercial_out_of_scope.append(dynamic)
        known_ids.update(_id_aliases(identifier))

    bidder_summary = (summary_fn or _summary)(check_records)
    vendor = _mapping(report_data.get("vendor"))
    bidder = {
        "bidder_id": report_data.get("vendor_id") or report_data.get("bidder_id") or vendor.get("id") or vendor.get("vendor_id"),
        "bidder_name": report_data.get("vendor_name") or report_data.get("bidder_name") or vendor.get("vendor_name") or vendor.get("name"),
    }
    return {
        "schema_version": profile.get("schema_version", "bhel-bid-evaluation.v1"),
        "profile_id": profile.get("profile_id", "bhel_purchase_materials"),
        "source": deepcopy(profile.get("source") or {}),
        "submission_policy": deepcopy(profile.get("submission_policy") or {}),
        "bidder": bidder,
        "checks": check_records,
        "internal_controls": controls,
        "commercial_out_of_scope": commercial_out_of_scope,
        "bidder_summary": bidder_summary,
    }


def _report_extension(report: Any) -> dict[str, Any]:
    report = _model_data(report)
    if not isinstance(report, Mapping):
        return {}
    extensions = _mapping(report.get("extensions"))
    return _mapping(extensions.get("bhel")) or _mapping(report.get("bhel")) or dict(report)


def bhel_comparative_summary(
    reports: dict[str, dict],
    *,
    load_profile: Callable[[], dict | None] = load_bhel_profile,
    summary_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict:
    """Summarize independent bidder extensions without mixing internal controls."""
    if load_profile() is None:
        return {}
    reports = _model_data(reports)
    reports = reports if isinstance(reports, dict) else {}
    bidder_rows: dict[str, dict[str, Any]] = {}
    criterion_counts = {status: 0 for status in RESULT_STATUSES}
    outcome_counts = {
        "qualified": 0,
        "not_qualified": 0,
        "review_required": 0,
        "not_evaluated": 0,
    }
    criteria: dict[str, dict[str, Any]] = {}
    for bidder_id, report in reports.items():
        extension = _report_extension(report)
        checks = _mapping_list(extension.get("checks"))
        if not checks:
            checks = _mapping_list(extension.get("criteria_results"))
        normalized_checks: list[dict[str, Any]] = []
        for check in checks:
            item = deepcopy(check)
            result = _normalized_result(item.get("result") or item.get("status")) or "needs_review"
            item["result"] = result
            item["blocks_qualification"] = bool(
                item.get("materiality") in _BLOCKING
                and result in {"not_met", "insufficient_evidence", "needs_review", "not_evaluated"}
            )
            normalized_checks.append(item)
            criterion_counts[result] += 1
            check_id = str(item.get("check_id") or item.get("criterion_id") or "").strip()
            if check_id:
                row = criteria.setdefault(
                    check_id,
                    {
                        "check_id": check_id,
                        "label": item.get("label") or check_id,
                        "requirement": item.get("requirement") or item.get("label") or check_id,
                        "origin": item.get("origin") or "tender_derived",
                        "materiality": item.get("materiality") or "mandatory_blocking",
                        "bidders": {},
                    },
                )
                row["bidders"][str(bidder_id)] = {
                    "result": result,
                    "value": deepcopy(item.get("value")),
                    "reason": item.get("reason") or "",
                    "source_references": deepcopy(item.get("source_references") or []),
                    "bidder_evidence": deepcopy(item.get("bidder_evidence") or []),
                    "blocks_qualification": item["blocks_qualification"],
                }
        summary = (summary_fn or _summary)(normalized_checks)
        outcome = summary["qualification"]
        if outcome not in outcome_counts:
            outcome = "not_evaluated"
        outcome_counts[outcome] += 1
        bidder_rows[str(bidder_id)] = {
            "bidder_name": extension.get("bidder", {}).get("bidder_name") if isinstance(extension.get("bidder"), Mapping) else None,
            "qualification": outcome,
            "counts": summary["counts"],
            "blocking_check_ids": summary["blocking_check_ids"],
            "evaluation_status": extension.get("evaluation_status") or "succeeded",
        }

    for row in criteria.values():
        for bidder_id in bidder_rows:
            row["bidders"].setdefault(
                bidder_id,
                {
                    "result": "not_evaluated",
                    "value": None,
                    "reason": "Bidder evaluation is not complete.",
                    "source_references": [],
                    "bidder_evidence": [],
                    "blocks_qualification": True,
                },
            )

    qualified = [key for key, row in bidder_rows.items() if row["qualification"] == "qualified"]
    not_qualified = [key for key, row in bidder_rows.items() if row["qualification"] == "not_qualified"]
    review = [key for key, row in bidder_rows.items() if row["qualification"] == "review_required"]
    not_evaluated = [key for key, row in bidder_rows.items() if row["qualification"] == "not_evaluated"]
    return {
        "schema_version": "bhel-bid-comparative.v1",
        "profile_id": "bhel_purchase_materials",
        "bidder_count": len(bidder_rows),
        "evaluated_bidder_count": len(bidder_rows) - len(not_evaluated),
        "criterion_counts": criterion_counts,
        "outcome_counts": outcome_counts,
        "qualified_bidders": qualified,
        "not_qualified_bidders": not_qualified,
        "needs_review_bidders": review,
        "not_evaluated_bidders": not_evaluated,
        "bidders": bidder_rows,
        "criteria": list(criteria.values()),
    }
