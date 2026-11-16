"""Runtime adapter for the canonical criterion decision ledger.

The proven evaluator still produces the established Pydantic report models.
This module gives those models one criterion-id keyed source of truth without
changing the public API envelope or importing the application monolith.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import re
from typing import Any

from procurement.evaluation.decision import (
    Assessment,
    CriterionDefinition,
    CriterionResult,
    DecisionLedger,
    EvidenceRef,
    build_decision_ledger,
)
from procurement.domain.schemas import Evidence


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump())
    return dict(vars(value))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _criterion_kind(data: Mapping[str, Any]) -> str:
    raw = _text(data.get("criterion_kind") or data.get("priority") or "gate").lower()
    scope = _text(data.get("evaluation_scope") or data.get("stage")).lower()
    effect = _text(data.get("decision_effect") or data.get("failure_effect")).lower()
    materiality = _text(data.get("materiality")).lower()
    # Decision semantics outrank a stale legacy kind. A criterion explicitly
    # marked advisory/non-blocking must never become a rejection gate merely
    # because an older serialized contract still says ``kind=gate``.
    if materiality in {"advisory", "reference_only"} or effect in {
        "advisory", "audit_only", "manual_review", "no_direct_fail",
    }:
        return "advisory"
    if raw in {"gate", "scored", "preference", "advisory", "post_award", "internal_gate"}:
        return raw
    if raw in {"internal_control", "internal-control"}:
        return "internal_gate"
    if raw in {"post-award", "post award"} or scope in {"post_award", "contract"}:
        return "post_award"
    if raw == "obligation":
        return "gate" if scope not in {"post_award", "contract"} else "post_award"
    if raw == "declaration" and effect not in {"no_direct_fail", "audit_only", "advisory"}:
        return "gate"
    if raw == "rollup":
        return "advisory"
    return "gate" if bool(data.get("mandatory", True)) else "advisory"


def _applicability(data: Mapping[str, Any]) -> str:
    value = data.get("applicability")
    if isinstance(value, Mapping):
        value = value.get("state")
    state = _text(value or "applicable").lower().replace(" ", "_")
    return state if state in {"applicable", "conditional", "not_applicable", "unknown"} else "unknown"


def _maximum_score(data: Mapping[str, Any]) -> float | None:
    for source in (data, data.get("threshold") if isinstance(data.get("threshold"), Mapping) else {}):
        for key in ("max_score", "maximum_score", "maximum", "marks"):
            value = source.get(key) if isinstance(source, Mapping) else None
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*marks?\b", _text(data.get("requirement")), re.I)
    return float(match.group(1)) if match else None


def criterion_definition(value: Any) -> CriterionDefinition:
    data = _dump(value)
    kind = _criterion_kind(data)
    required = data.get("required_documents") or data.get("evidence_requirements") or []
    if required and isinstance(required[0] if isinstance(required, list) else None, Mapping):
        required = [
            _text(item.get("document_name") or item.get("display_name"))
            for item in required
            if isinstance(item, Mapping)
        ]
    return CriterionDefinition(
        criterion_id=_text(data.get("criterion_id") or data.get("id")),
        requirement=_text(data.get("requirement") or data.get("criterion_text")),
        category=_text(data.get("category") or "other"),
        criterion_kind=kind,
        evaluation_scope=_text(data.get("evaluation_scope") or "technical_qualification"),
        materiality=_text(data.get("materiality") or ""),
        decision_effect=_text(data.get("decision_effect") or data.get("failure_effect") or ""),
        applicability=_applicability(data),
        active=not bool(data.get("excluded", False)),
        threshold=dict(data.get("threshold") or {}),
        pass_rule=_text(data.get("pass_rule") or data.get("pass_condition")),
        evidence_requirements=tuple(_text(item) for item in required if _text(item)),
        depends_on=tuple(_text(item) for item in data.get("depends_on") or [] if _text(item)),
        max_score=_maximum_score(data),
        metadata={"legacy_mandatory": bool(data.get("mandatory", True))},
    )


def _page_inventory(documents: Iterable[Any]) -> tuple[dict[str, Any], dict[tuple[str, int], Any]]:
    by_id: dict[str, Any] = {}
    by_document_page: dict[tuple[str, int], Any] = {}
    for document in documents:
        record = getattr(document, "record", None)
        document_id = _text(getattr(record, "document_id", ""))
        for page in getattr(document, "pages", ()) or ():
            page_id = _text(getattr(page, "page_id", ""))
            page_number = int(getattr(page, "page_number", 0) or 0)
            if page_id:
                by_id[page_id] = page
            if document_id and page_number > 0:
                by_document_page[(document_id, page_number)] = page
    return by_id, by_document_page


def _page_ref(
    document_id: str,
    page_number: int | None,
    *,
    quote: str = "",
    image_ref: str = "",
    page_id: str = "",
    pages_by_id: Mapping[str, Any],
    pages_by_document_page: Mapping[tuple[str, int], Any],
    strict: bool,
) -> EvidenceRef | None:
    page = pages_by_id.get(page_id) if page_id else pages_by_document_page.get((document_id, page_number or 0))
    if page is not None:
        document_id = document_id or _text(getattr(page, "document_id", ""))
        page_number = int(page_number or getattr(page, "page_number", 0) or 0)
    if not document_id or not page_number or page_number < 1:
        return None
    exact_quote = _text(quote)
    if exact_quote and page is not None:
        page_text = _normalized(getattr(page, "text", ""))
        # Native extract_text on scanned eOffice pages is often only a header.
        # An OCR/model quote on a known submitted page still grounds the pass.
        if page_text and _normalized(exact_quote) not in page_text:
            thin = len(page_text) < 400 or "generated from eoffice" in page_text
            if not thin:
                exact_quote = ""
    if exact_quote:
        return EvidenceRef(
            document_id=document_id,
            page=page_number,
            page_id=page_id or None,
            quote=exact_quote,
        )
    if image_ref:
        return EvidenceRef(
            document_id=document_id,
            page=page_number,
            page_id=page_id or None,
            image_ref=image_ref,
        )
    kept_quote = _text(quote)
    if page is not None and kept_quote:
        return EvidenceRef(
            document_id=document_id,
            page=page_number,
            page_id=page_id or None,
            quote=kept_quote,
        )
    if page is not None:
        return EvidenceRef(
            document_id=document_id,
            page=page_number,
            page_id=page_id or None,
            locator=page_id or f"{document_id}:p{page_number}",
        )
    if not strict and kept_quote:
        return EvidenceRef(document_id=document_id, page=page_number, quote=kept_quote)
    return None


def _evidence_refs(
    finding: Mapping[str, Any],
    verifications: Sequence[Any],
    *,
    pages_by_id: Mapping[str, Any],
    pages_by_document_page: Mapping[tuple[str, int], Any],
    strict: bool,
) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for raw in finding.get("evidence") or []:
        source = _dump(raw)
        ref = _page_ref(
            _text(source.get("document_id")),
            source.get("page") or source.get("page_number"),
            quote=_text(source.get("quote")),
            pages_by_id=pages_by_id,
            pages_by_document_page=pages_by_document_page,
            strict=strict,
        )
        if ref:
            refs.append(ref)
    for verification_value in verifications:
        verification = _dump(verification_value)
        quote = _text(verification.get("quote"))
        selected_page_ids = {
            _text(page_id)
            for page_id in verification.get("page_ids") or []
            if _text(page_id)
        }
        for page_id in verification.get("page_ids") or []:
            page = pages_by_id.get(_text(page_id))
            if page is None:
                continue
            ref = _page_ref(
                _text(getattr(page, "document_id", "")),
                int(getattr(page, "page_number", 0) or 0),
                quote=quote,
                page_id=_text(page_id),
                pages_by_id=pages_by_id,
                pages_by_document_page=pages_by_document_page,
                strict=strict,
            )
            if ref:
                refs.append(ref)
        for check in verification.get("visual_checks") or []:
            if not isinstance(check, Mapping) or _text(check.get("status")).lower() != "inspected":
                continue
            page_id = _text(check.get("page_id"))
            if page_id not in selected_page_ids:
                continue
            page = pages_by_id.get(page_id)
            if page is None:
                continue
            ref = _page_ref(
                _text(getattr(page, "document_id", "")),
                int(getattr(page, "page_number", 0) or 0),
                image_ref=page_id,
                page_id=page_id,
                pages_by_id=pages_by_id,
                pages_by_document_page=pages_by_document_page,
                strict=strict,
            )
            if ref:
                refs.append(ref)
    rich_pages = {
        (ref.document_id, ref.page, ref.page_id)
        for ref in refs
        if ref.quote or ref.image_ref
    }
    unique: dict[str, EvidenceRef] = {}
    for ref in refs:
        if not (ref.quote or ref.image_ref) and (
            ref.document_id,
            ref.page,
            ref.page_id,
        ) in rich_pages:
            continue
        key = repr(sorted(ref.to_dict().items()))
        unique[key] = ref
    return tuple(unique.values())


def _result_state(finding: Mapping[str, Any], criterion: CriterionDefinition) -> str:
    status = _text(finding.get("status")).lower()
    reason = _normalized(finding.get("reason"))
    applicability_status = _text(finding.get("applicability_status")).lower()
    if criterion.applicability == "not_applicable":
        return "not_applicable"
    if applicability_status == "not_applicable":
        return "not_applicable"
    if status in {"not_applicable", "not_evaluated"} and any(
        marker in reason
        for marker in ("do not apply", "does not apply", "not entitled", "not applicable")
    ):
        return "not_applicable"
    if any(marker in reason for marker in (
        "complete semantic search coverage", "search incomplete", "remain uninspected",
        "provider unavailable", "provider timed out", "unreadable page",
    )):
        return "search_incomplete"
    if "conflict" in reason or "mismatch" in reason:
        return "conflicting"
    return {
        "pass": "passed",
        "passed": "passed",
        "fail": "failed",
        "failed": "failed",
        "missing": "missing",
        "unclear": "needs_human_review",
        "not_applicable": "not_applicable",
        "not_evaluated": "not_evaluated_after_blocker" if "after" in reason else "not_evaluated",
    }.get(status, "needs_human_review")


def build_runtime_ledger(
    criteria: Sequence[Any],
    findings: Sequence[Any],
    *,
    documents: Sequence[Any] = (),
    verifications: Sequence[Any] = (),
    tender_version_id: str = "",
    bid_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> DecisionLedger:
    definitions = tuple(criterion_definition(item) for item in criteria)
    findings_by_id = {_text(_dump(item).get("criterion_id")): _dump(item) for item in findings}
    verifications_by_id: dict[str, list[Any]] = {}
    for item in verifications:
        data = _dump(item)
        verifications_by_id.setdefault(_text(data.get("criterion_id")), []).append(item)
    pages_by_id, pages_by_document_page = _page_inventory(documents)
    strict = bool(pages_by_id or pages_by_document_page)
    results: list[CriterionResult] = []
    for criterion in definitions:
        finding = findings_by_id.get(criterion.criterion_id)
        if finding is None:
            assessment = Assessment(
                state="not_evaluated",
                reason="No result was produced for this active frozen criterion.",
            )
            results.append(CriterionResult(criterion_id=criterion.criterion_id, ai_assessment=assessment))
            continue
        state = _result_state(finding, criterion)
        supporting_verifications = [
            verification
            for verification in verifications_by_id.get(criterion.criterion_id, ())
            if _text(_dump(verification).get("status")).lower()
            == ("pass" if state == "passed" else "fail" if state == "failed" else "")
        ]
        # The criterion finding and verifier are independent evidence producers.
        # Do not discard an exact finding citation merely because a verifier also
        # ran: scanned pages can yield a grounded OCR/VLM citation while the
        # verifier remains inconclusive or omits its page_ids.
        finding_for_refs = dict(finding)
        refs = _evidence_refs(
            finding_for_refs,
            supporting_verifications,
            pages_by_id=pages_by_id,
            pages_by_document_page=pages_by_document_page,
            strict=strict,
        )
        reason = _text(finding.get("reason") or "Evaluated from submitted bidder evidence.")
        if state == "passed" and not refs:
            state = "needs_human_review"
            reason = f"{reason} The pass could not be tied to an exact submitted page or image."
        assessment = Assessment(
            state=state,
            objective_answer=reason.split(".", 1)[0],
            reason=reason,
            evidence_refs=refs,
            metadata={"original_status": _text(finding.get("status"))},
        )
        results.append(
            CriterionResult(
                criterion_id=criterion.criterion_id,
                ai_assessment=assessment,
                maximum_score=criterion.max_score,
                metadata={
                    "missing_documents": list(finding.get("missing_documents") or []),
                    "matched_documents": list(finding.get("matched_documents") or []),
                },
            )
        )
    return build_decision_ledger(
        definitions,
        results,
        tender_version_id=tender_version_id,
        bid_id=bid_id,
        metadata=dict(metadata or {}),
    )


def apply_ledger_to_report(report: Any, ledger: DecisionLedger) -> Any:
    """Apply only established report fields; the external envelope stays stable."""
    projection = ledger.compatibility_projection()
    report.qualifies = bool(projection["qualifies"])
    report.recommendation = projection["recommendation"]
    report.final_decision = projection["final_decision"]
    report.summary = projection["summary"]
    report.extensions["canonical_decision_ledger"] = {
        "payload": ledger.canonical_payload(),
        "sha256": ledger.canonical_hash(),
    }
    results = ledger.result_by_criterion_id
    criteria = {item.criterion_id: item for item in ledger.criteria}
    findings_by_id = {item.criterion_id: item for item in report.findings}
    for criterion_id, result in results.items():
        finding = findings_by_id.get(criterion_id)
        if finding is None:
            continue
        projected = next(item for item in ledger.compatibility_findings() if item["criterion_id"] == criterion_id)
        finding.status = projected["status"]
        finding.reason = projected["reason"]
        finding.missing_documents = list(projected["missing_documents"])
        definition = criteria[criterion_id]
        finding.mandatory = bool(projected["mandatory"])
        finding.criterion_kind = definition.criterion_kind
        finding.evaluation_scope = definition.evaluation_scope
        finding.materiality = _text(definition.materiality)
        finding.decision_effect = _text(definition.decision_effect)
        finding.evidence = [
            Evidence(
                document_id=ref.document_id,
                file_name=ref.file_name,
                page=ref.page,
                quote=ref.quote,
                confidence=0.8,
            )
            for ref in result.effective_assessment.evidence_refs
        ]
    review_ids = ledger.technical_qualification.get("review_criterion_ids") or []
    report.manual_review_reasons = [
        results[criterion_id].effective_assessment.reason
        for criterion_id in review_ids
        if criterion_id in results
    ]
    coverage_gaps = list(ledger.metadata.get("unautomated_requirements") or [])
    report.extensions["unautomated_requirements"] = coverage_gaps
    report.manual_review_reasons.extend(
        "Tender requirement needs human review: " + str(item.get("requirement") or item.get("source_id") or "Unresolved source coverage")
        + (" — " + str(item["reason"]) if item.get("reason") else "")
        for item in coverage_gaps
    )
    report.risk_flags = list(report.manual_review_reasons)
    report.missing_mandatory = sorted({
        document
        for criterion_id in ledger.technical_qualification.get("blocking_criterion_ids") or []
        for document in results[criterion_id].metadata.get("missing_documents", ())
    })
    technical_state = ledger.technical_qualification["state"]
    report.technical_result.status = {
        "qualified": "pass",
        "not_qualified": "fail",
        "review_required": "unclear",
        "not_evaluated": "unclear",
    }[technical_state]
    report.technical_result.reason = ledger.technical_qualification["reason"]
    report.technical_result.failed_criteria = list(ledger.technical_qualification.get("blocking_criterion_ids") or [])
    opening_state = ledger.financial_opening["state"]
    report.financial_result.status = "pass" if opening_state == "allowed" else "fail" if opening_state == "blocked" else "unclear"
    report.financial_result.opening_allowed = bool(ledger.financial_opening["allowed"])
    report.financial_result.reason = ledger.financial_opening["reason"]
    report.responsiveness.status = (
        "responsive" if technical_state == "qualified"
        else "unresponsive" if technical_state == "not_qualified"
        else "clarification_required"
    )
    return report


__all__ = [
    "apply_ledger_to_report",
    "build_runtime_ledger",
    "criterion_definition",
]
