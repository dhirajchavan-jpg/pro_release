"""Evaluation presentation, decision summaries, and human-review overlays."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from procurement.domain.models import Evaluation, Review
from procurement.domain.text import humanize_check_name
from procurement.evaluation.validation import normalize_status_reason
from procurement.workbench.common_project import _preview_is_pdf


def _match_source_pages(
    quote: str,
    page_ids: list[str],
    pages_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    tokens = re.findall(r"[a-z0-9]+", quote.casefold())
    quote_tokens = set(tokens)
    matches: list[tuple[float, dict[str, Any]]] = []
    for page_id in page_ids:
        page = pages_by_id.get(page_id) or {}
        text = " ".join(re.findall(r"[a-z0-9]+", str(page.get("text") or "").casefold()))
        if not text:
            continue
        exact = " ".join(tokens) in text if tokens else False
        overlap = len(quote_tokens & set(text.split())) / len(quote_tokens) if quote_tokens else 0
        if exact or overlap >= 0.7:
            matches.append((1.0 if exact else overlap, page))
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return []
    cutoff = max(0.7, matches[0][0] - 0.08)
    return [page for score, page in matches if score >= cutoff][:3]


def _plain_reason(
    value: Any,
    pages_by_id: dict[str, dict[str, Any]],
    documents: dict[str, dict[str, Any]],
) -> str:
    text = str(value or "").strip()
    replacements = {
        "proof contract": "required form of evidence",
        "artifact proof": "documentary evidence",
        "artifact label": "document type",
        "artifact": "submitted document",
        "semantic search coverage": "search coverage across the bidder documents",
        "semantic search": "search across the bidder documents",
        "provenance": "document source and authenticity",
    }
    for internal, plain in replacements.items():
        text = re.sub(re.escape(internal), plain, text, flags=re.IGNORECASE)
    for page_id, page in pages_by_id.items():
        if page_id not in text:
            continue
        document_id = str(page.get("document_id") or "")
        file_name = str((documents.get(document_id) or {}).get("file_name") or page.get("file_name") or "Bid document")
        page_number = page.get("page_number") or page.get("page")
        text = text.replace(page_id, f"{file_name}, page {page_number}" if page_number else file_name)
    return text


def _normalize_public_report(report: dict[str, Any]) -> dict[str, Any]:
    result = dict(report)
    result["criteria_results"] = [
        {
            **item,
            "why": normalize_status_reason(
                item.get("status"), item.get("why") or item.get("result") or ""
            ),
        }
        if isinstance(item, dict)
        else item
        for item in report.get("criteria_results") or []
    ]
    return result


def _reconciled_findings(
    findings: list[dict[str, Any]],
    verifications: dict[str, list[dict[str, Any]]],
    evidence_names: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    reconciled: list[dict[str, Any]] = []
    for original in findings:
        finding = dict(original)
        criterion_id = str(finding.get("criterion_id") or "")
        items = verifications.get(criterion_id) or []
        if not items:
            reconciled.append(finding)
            continue
        names_by_status: dict[str, list[str]] = {"pass": [], "fail": [], "missing": [], "unclear": []}
        for verification in items:
            status = str(verification.get("status") or "unclear").casefold()
            status = status if status in names_by_status else "unclear"
            evidence_id = str(verification.get("evidence_id") or "")
            evidence = evidence_names.get((criterion_id, evidence_id)) or {}
            name = str(evidence.get("document_name") or evidence_id)
            if status == "missing":
                from procurement.evaluation.evidence_pipeline_specialists import specialist_confirms_document_identity

                if any(
                    specialist.get("status") == "pass"
                    and specialist_confirms_document_identity(evidence, specialist)
                    for specialist in verification.get("specialist_findings") or []
                ):
                    status = "unclear"
            if name and name not in names_by_status[status]:
                names_by_status[status].append(name)
        failed = names_by_status["fail"]
        missing = names_by_status["missing"]
        unclear = names_by_status["unclear"]
        finding["missing_documents"] = missing
        if failed:
            finding["status"] = "fail"
            finding["reason"] = "Submitted evidence does not meet the requirement for: " + ", ".join(failed) + "."
        elif missing:
            finding["status"] = "missing"
            finding["reason"] = "Missing required evidence: " + ", ".join(missing) + "."
            if unclear:
                finding["reason"] += " Additional evidence needs review: " + ", ".join(unclear) + "."
        elif unclear:
            finding["status"] = "unclear"
            finding["reason"] = "Evidence needs review for: " + ", ".join(unclear) + "."
        elif names_by_status["pass"]:
            finding["status"] = "pass"
            finding["reason"] = "All required evidence for this check was verified."
        reconciled.append(finding)
    return reconciled


def _project_verifications(
    verifications: dict[str, list[dict[str, Any]]],
    evidence_names: dict[tuple[str, str], dict[str, Any]],
    packets: dict[tuple[str, str], dict[str, Any]],
    pages_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    from procurement.evaluation.evidence_pipeline_specialists import (
        annexure_labels,
        declaration_subject_matches,
        specialist_confirms_document_identity,
    )

    projected: dict[str, list[dict[str, Any]]] = {}
    for criterion_id, items in verifications.items():
        for original in items:
            verification = dict(original)
            evidence_id = str(verification.get("evidence_id") or "")
            evidence = evidence_names.get((criterion_id, evidence_id)) or {}
            name = str(evidence.get("document_name") or evidence_id)
            quote = str(verification.get("quote") or "")
            candidate_page_ids = [str(page_id) for page_id in verification.get("page_ids") or []]
            if str(verification.get("status") or "").casefold() == "missing":
                exact_specialist = next((
                    specialist
                    for specialist in verification.get("specialist_findings") or []
                    if specialist.get("status") == "pass"
                    and specialist_confirms_document_identity(evidence, specialist)
                ), None)
                if exact_specialist:
                    verification["status"] = "unclear"
                    quote = str(exact_specialist.get("quote") or quote)
                    verification["quote"] = quote
                    candidate_page_ids = [str(page_id) for page_id in exact_specialist.get("page_ids") or []]
                    checks = dict(verification.get("check_results") or {})
                    checks["presence"] = "pass"
                    verification["check_results"] = checks
                    verification["reason"] = f"{name} was located, but its document-specific checks still need review."
            packet = packets.get((criterion_id, evidence_id)) or {}
            matched = _match_source_pages(
                quote,
                list(dict.fromkeys([
                    *candidate_page_ids,
                    *[str(page_id) for page_id in packet.get("page_ids") or []],
                ])),
                pages_by_id,
            ) if quote else []
            if any(term in name.casefold() for term in ("annexure", "declaration", "undertaking", "certificate")):
                all_candidate_pages = [
                    pages_by_id[page_id]
                    for page_id in list(dict.fromkeys([
                        *candidate_page_ids,
                        *[str(page_id) for page_id in packet.get("page_ids") or []],
                    ]))
                    if page_id in pages_by_id
                ]
                def usable_subject_page(page: dict[str, Any]) -> bool:
                    text = str(page.get("text") or "")
                    return declaration_subject_matches(name, text) and (
                        len(annexure_labels(text)) <= 2
                        or any(
                            declaration_subject_matches(name, line)
                            and re.search(r"\bapplicable\b.{0,40}\b(?:yes|no)\b", line, flags=re.IGNORECASE)
                            for line in text.splitlines()
                        )
                    )

                matched = [
                    page for page in matched
                    if usable_subject_page(page)
                ][:3]
                if declaration_subject_matches(name, name) and not matched:
                    matched = [page for page in all_candidate_pages if usable_subject_page(page)][:3]
            checklist_no = next((
                line.strip()
                for page in matched
                for line in str(page.get("text") or "").splitlines()
                if declaration_subject_matches(name, line)
                and re.search(r"\bapplicable\b.{0,40}\bno\b", line, flags=re.IGNORECASE)
            ), None)
            if checklist_no:
                matched = [
                    page for page in matched
                    if checklist_no in str(page.get("text") or "")
                ][:1]
                verification["status"] = "missing"
                verification["quote"] = checklist_no
                quote = checklist_no
                checks = dict(verification.get("check_results") or {})
                checks["presence"] = "missing"
                verification["check_results"] = checks
                verification["reason"] = f"The bidder's submitted checklist marks {name} as not enclosed, and no matching submitted form was verified."
            elif matched:
                expected_labels = annexure_labels(name)
                actual_labels = set().union(*[
                    annexure_labels(str(page.get("text") or "")) for page in matched
                ])
                if expected_labels and actual_labels and not (expected_labels & actual_labels):
                    expected = ", ".join(sorted(expected_labels))
                    actual = max(actual_labels, key=lambda value: int(re.match(r"\d+", value).group()))
                    verification["status"] = "unclear"
                    checks = dict(verification.get("check_results") or {})
                    checks["tender_requirement_match"] = "unclear"
                    verification["check_results"] = checks
                    verification["reason"] = (
                        f"Matching declaration content was found, but it is labelled Annexure {actual} "
                        f"rather than the tender's Annexure {expected}; confirm that the submitted form is equivalent."
                    )
            verification["page_ids"] = [str(page.get("page_id") or "") for page in matched if page.get("page_id")]
            checks = dict(verification.get("check_results") or {})
            unresolved = {
                str(check)
                for check, state in checks.items()
                if str(state).casefold() in {"fail", "missing", "unclear"}
                and str(check) != "specialist_content"
            }
            if verification.get("status") == "unclear" and unresolved == {"provenance"} and matched:
                verification["status"] = "pass"
                checks["provenance"] = "pass"
                verification["check_results"] = checks
                verification["reason"] = f"Passed: {name} was tied to the cited submitted page."
            projected.setdefault(criterion_id, []).append(verification)
    return projected


def _tender_source_refs(
    tender: dict[str, Any],
    understanding: dict[str, Any],
    project_id: str,
) -> list[dict[str, Any]]:
    quote = str(tender.get("source_quote") or "")
    if not quote:
        return []
    units = understanding.get("source_units") or {}
    extracted_pages = {
        str(page.get("page_id") or ""): page
        for page in understanding.get("extracted_units") or []
        if isinstance(page, dict) and page.get("page_id")
    }
    source_pages = extracted_pages or {
        str(unit.get("page_id") or ""): {
            **unit,
            "text": unit.get("text") or "",
            "document_id": str(unit.get("page_id") or "").rsplit("-p", 1)[0],
            "page_number": unit.get("page"),
        }
        for unit in units.values()
        if isinstance(unit, dict) and unit.get("page_id")
    }
    matched = _match_source_pages(
        quote,
        list(source_pages),
        source_pages,
    )
    refs = []
    for page in matched:
        source_id = str(page.get("document_id") or "")
        page_number = int(page.get("page_number") or page.get("page") or 0)
        preview = (understanding.get("preview_sources") or {}).get(source_id) or {}
        base = f"/v1/workbench/projects/{project_id}/sources/{source_id}"
        available = bool(preview.get("object_key"))
        refs.append(
            {
                "document_id": source_id,
                "file_name": preview.get("file_name") or page.get("source_file") or "Tender source",
                "page": page_number,
                "quote": quote,
                "page_image_url": f"{base}/pages/{page_number}.png"
                if available and _preview_is_pdf(preview)
                else None,
                "document_url": f"{base}/content" if available else None,
            }
        )
    return refs or [
        {
            "document_id": tender.get("source_document_id") or "tender",
            "file_name": "Tender source",
            "page": tender.get("source_page_number"),
            "quote": quote,
            "page_image_url": None,
            "document_url": None,
            "source_note": "The reviewed quote was retained, but its source page could not be resolved.",
        }
    ]


def _ui_evaluation(
    evaluation: Evaluation | None,
    *,
    project_id: str = "",
    vendor_key: str = "",
    intake: dict[str, Any] | None = None,
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not evaluation or evaluation.status != "succeeded":
        return {
            "status": evaluation.status if evaluation else "not_started",
            "recommendation": "needs_manual_review",
            "criteria": [],
        }
    stored = dict(evaluation.internal_result_json or {})
    internal = stored.get("report") or {}
    if internal:
        intake = intake or {}
        understanding = understanding or {}
        handoffs = stored.get("handoffs") or {}
        tender_criteria = {
            item.get("criterion_id"): item for item in (internal.get("tender") or {}).get("criteria") or []
        }
        documents = {
            str(item.get("document_id") or ""): item
            for item in intake.get("documents") or internal.get("documents") or []
        }
        previews = intake.get("preview_sources") or {}
        pages_by_id = {
            str(item.get("page_id") or ""): item
            for item in intake.get("pages") or []
            if isinstance(item, dict)
        }
        packets = {
            (str(item.get("criterion_id") or ""), str(item.get("evidence_id") or "")): item
            for item in (handoffs.get("06a_evidence_packets") or {}).get("packets") or []
        }
        evidence_names = {
            (str(item.get("criterion_id") or ""), str(item.get("evidence_id") or "")): item
            for item in (handoffs.get("06_criteria_evidence_map") or {}).get("entries") or []
        }
        verifications: dict[str, list[dict[str, Any]]] = {}
        for item in (handoffs.get("07_document_verification") or {}).get("items") or []:
            verifications.setdefault(str(item.get("criterion_id") or ""), []).append(item)
        verifications = _project_verifications(verifications, evidence_names, packets, pages_by_id)
        from procurement.understanding.requirement_evidence import evidence_for_reviewed_atomic_field

        for criterion_id, items in list(verifications.items()):
            tender = tender_criteria.get(criterion_id) or {}
            reviewed = ((tender.get("extensions") or {}).get("reviewed_field") or {})
            evidence_documents = [
                str((evidence_names.get((criterion_id, str(item.get("evidence_id") or ""))) or {}).get("document_name") or "")
                for item in items
            ]
            routed = evidence_for_reviewed_atomic_field(
                str(reviewed.get("field_key") or ""),
                str(tender.get("requirement") or ""),
                evidence_documents,
                str(reviewed.get("atomic_value") or ""),
                list(tender.get("source_references") or []),
            ) if reviewed.get("field_key") and reviewed.get("atomic_value") else []
            routed_names = {str(item.get("document_name") or "").casefold() for item in routed}
            if routed_names and len(routed_names) < len({document.casefold() for document in evidence_documents}):
                verifications[criterion_id] = [
                    item for item, document in zip(items, evidence_documents)
                    if document.casefold() in routed_names
                ]
        from procurement.evaluation.next_ask import apply_next_ask, cards_by_criterion
        public_findings = list(internal.get("findings") or [])
        canonical_ledger = (
            ((internal.get("extensions") or {}).get("canonical_decision_ledger") or {}).get("payload")
        )
        if not canonical_ledger:
            public_findings = _reconciled_findings(
                public_findings, verifications, evidence_names,
            )
        public_findings = [
            {
                **finding,
                "reason": normalize_status_reason(finding.get("status"), finding.get("reason")),
            }
            for finding in public_findings
        ]
        reviewed_tender = dict(internal.get("tender") or {})
        reviewed_fields = understanding.get("field_understanding")
        if isinstance(reviewed_fields, dict) and reviewed_fields.get("sections"):
            from procurement.understanding.service import profile_from_fields

            projected = profile_from_fields(reviewed_fields)
            reviewed_tender.update(
                {
                    key: projected[key]
                    for key in ("procurement_type", "bid_system", "evaluation_method")
                    if projected.get(key) not in (None, "", "Unknown")
                }
            )
        next_ask_store = apply_next_ask(
            public_findings,
            vendor=internal.get("vendor") or {},
            tender=reviewed_tender,
        )

        next_ask_by_id = cards_by_criterion(next_ask_store)
        criteria = []
        for finding in public_findings:
            criterion_id = finding.get("criterion_id") or ""
            tender = tender_criteria.get(criterion_id) or {}
            claims = []
            verification_details = []
            ai_items = verifications.get(criterion_id) or []
            for index, verification in enumerate(ai_items, start=1):
                evidence_id = str(verification.get("evidence_id") or "")
                evidence = evidence_names.get((criterion_id, evidence_id)) or {}
                evidence_name = evidence.get("document_name") or f"Evidence item {index}"
                display_status = str(verification.get("status") or "unclear")
                display_reason = _plain_reason(
                    normalize_status_reason(display_status, verification.get("reason")),
                    pages_by_id,
                    documents,
                )
                quote = str(verification.get("quote") or "")
                packet = packets.get((criterion_id, evidence_id)) or {}
                verified_page_ids = [str(page_id) for page_id in verification.get("page_ids") or []]
                matched_pages = [
                    pages_by_id[page_id]
                    for page_id in verified_page_ids
                    if page_id in pages_by_id
                ][:3]
                source_refs = []
                for page in matched_pages:
                    document_id = str(page.get("document_id") or "")
                    document = documents.get(document_id) or {}
                    preview = previews.get(document_id) or {}
                    page_number = int(page.get("page_number") or 0)
                    base = (
                        f"/v1/workbench/projects/{project_id}/bids/{vendor_key}"
                        f"/sources/{document_id}"
                    )
                    source_refs.append(
                        {
                            "document_id": document_id,
                            "file_name": document.get("file_name") or page.get("file_name") or document_id,
                            "page": page_number,
                            "quote": quote,
                            "page_image_url": f"{base}/pages/{page_number}.png"
                            if preview.get("object_key") and _preview_is_pdf(preview)
                            else None,
                            "document_url": f"{base}/content" if preview.get("object_key") else None,
                        }
                    )
                failed_checks = [
                    {
                        "provenance": "exact source page",
                        "tender_requirement_match": "match to this tender requirement",
                        "signed": "required signature",
                        "stamped": "required stamp",
                    }.get(str(check), humanize_check_name(str(check)))
                    for check, result in (verification.get("check_results") or {}).items()
                    if str(result).casefold() in {"fail", "missing", "unclear"}
                    and str(check) != "specialist_content"
                ]
                verification_details.append({
                    "name": evidence_name,
                    "status": display_status,
                    "reason": display_reason,
                    "failed_checks": failed_checks,
                    "source_refs": source_refs,
                    "search_coverage": evidence.get("search_coverage") or {},
                })
                claims.append(
                    {
                        "fact_name": evidence_name,
                        "value": quote or display_reason or "No supporting quote returned",
                        "source_refs": source_refs,
                        "extraction_method": "ai_document_verifier",
                        "role_id": "vendor_submission",
                        "verification_status": display_status,
                        "verification_reason": display_reason,
                    }
                )
            claims = [claim for claim in claims if claim["source_refs"]]
            missing_documents = [str(value) for value in finding.get("missing_documents") or [] if str(value).strip()]
            applicability_status = str(finding.get("applicability_status") or "applicable")
            applicability_reason = str(finding.get("applicability_reason") or "").strip()
            if applicability_status == "unclear" and applicability_reason:
                issue = _plain_reason(applicability_reason, pages_by_id, documents)
            elif missing_documents:
                issue = "Missing required evidence: " + ", ".join(missing_documents) + "."
            else:
                issue = _plain_reason(finding.get("reason") or "The criterion needs review.", pages_by_id, documents)

            observed = []
            for detail in verification_details:
                refs = detail["source_refs"]
                locations = [
                    f"{ref.get('file_name') or 'Bid document'}, page {ref.get('page')}"
                    for ref in refs
                    if ref.get("page")
                ]
                location = f" ({'; '.join(locations)})" if locations else ""
                status = str(detail["status"])
                generic_reason = any(
                    phrase in str(detail["reason"]).casefold()
                    for phrase in (
                        "proposed decision could not be tied",
                        "declaration specialist found declaration/undertaking evidence",
                    )
                )
                if status == "missing":
                    if detail["reason"] and refs:
                        line = str(detail["reason"])
                    else:
                        exhaustive = bool(detail["search_coverage"].get("verification_search_exhausted"))
                        scope = "after all readable bidder pages were searched" if exhaustive else "in the bidder submission"
                        line = f"No acceptable {detail['name']} was found {scope}."
                elif generic_reason:
                    checks = ", ".join(detail["failed_checks"])
                    line = (
                        f"{detail['name']} was located, but these checks could not be confirmed: {checks}."
                        if checks
                        else f"Declaration-like text was located, but it was not confirmed as the exact required document: {detail['name']}."
                    )
                elif detail["reason"]:
                    line = str(detail["reason"])
                elif detail["failed_checks"]:
                    line = f"{detail['name']} was located, but these checks remain unresolved: {', '.join(detail['failed_checks'])}."
                else:
                    line = str(detail["reason"] or f"{detail['name']} was assessed as {status}.")
                observed.append(line + location)

            check = {
                "criterion_id": criterion_id,
                "checkpoint_id": criterion_id,
                "title": finding.get("criterion_text") or tender.get("requirement") or criterion_id,
                "requirement": tender.get("requirement") or finding.get("criterion_text") or "",
                "required_proof": ", ".join(finding.get("required_documents") or []) or "Tender-required bidder evidence",
                "family": finding.get("category") or tender.get("category") or "other",
                "status": finding.get("status") or "unclear",
                "evaluation_state": (
                    "not_evaluated_system"
                    if finding.get("status") == "not_evaluated"
                    else "evaluated"
                ),
                "reason": finding.get("reason") or "",
                "observed_value": finding.get("reason") or (claims[0]["value"] if claims else ""),
                "claims": claims,
                "review_codes": ["missing_evidence"] if not claims else [],
                "tender_evidence": _tender_source_refs(tender, understanding, project_id),
                "ai_verifications": ai_items,
                "finding_explanation": {
                    "requirement": tender.get("requirement") or finding.get("criterion_text") or "",
                    "issue": issue,
                    "observed": observed,
                    "missing_documents": missing_documents,
                    "applicability_status": applicability_status,
                    "applicability_reason": applicability_reason,
                },
            }
            stored_card = next_ask_by_id.get(str(criterion_id))
            if stored_card:
                check["next_ask"] = stored_card
                action = stored_card.get("next_action") if isinstance(stored_card, dict) else None
                if isinstance(action, dict):
                    check["finding_explanation"]["reviewer_action"] = " ".join(
                        value for value in [
                            _plain_reason(action.get("label"), pages_by_id, documents),
                            _plain_reason(action.get("reason"), pages_by_id, documents),
                        ]
                        if value
                    )
            if str(check["status"]).casefold() in {"pass", "passed", "compliant", "not_applicable"}:
                check["finding_explanation"]["reviewer_action"] = "No action required."
            elif not check["finding_explanation"].get("reviewer_action"):
                if applicability_status == "unclear":
                    applicability = tender.get("applicability")
                    applicability = applicability if isinstance(applicability, dict) else {}
                    rule = str(applicability.get("rule") or "the tender condition")
                    check["finding_explanation"]["reviewer_action"] = f"Confirm from the bidder submission whether {rule}."
                elif missing_documents:
                    check["finding_explanation"]["reviewer_action"] = "Verify or obtain the missing evidence: " + ", ".join(missing_documents) + "."
                else:
                    check["finding_explanation"]["reviewer_action"] = "Review the cited bidder evidence against this tender requirement."
            criteria.append(
                {
                    "criterion_id": criterion_id,
                    "family": check["family"],
                    "status": check["status"],
                    "checks": [check],
                }
            )
        return {
            "status": evaluation.status,
            "recommendation": internal.get("recommendation") or "needs_manual_review",
            "final_decision": internal.get("final_decision") or evaluation.final_decision,
            "criteria": criteria,
            "decision_summary": decision_summary(
                criteria,
                internal.get("final_decision") or evaluation.final_decision,
                internal.get("qualifies"),
            ),
        }
    public = evaluation.result_json or {}
    return {
        "status": evaluation.status,
        "recommendation": public.get("recommendation") or "needs_manual_review",
        "final_decision": public.get("final_decision"),
        "criteria": [],
        "decision_summary": decision_summary(
            public.get("criteria_results") or [],
            public.get("final_decision"),
            public.get("qualifies"),
        ),
    }


def decision_summary(
    criteria: list[dict[str, Any]],
    final_decision: str | None = None,
    qualifies: bool | None = None,
) -> dict[str, Any]:
    counts = {"passed": 0, "failed": 0, "review": 0, "not_evaluated": 0}
    requirements = {
        "passed": [],
        "failed": [],
        "review": [],
        "not_evaluated": [],
    }
    pass_statuses = {"pass", "passed", "qualified", "compliant", "not_applicable"}
    fail_statuses = {
        "fail", "failed", "fail_fact", "present_but_invalid",
    }
    ask_statuses = {
        "missing", "missing_after_complete_search",
    }
    for criterion in criteria:
        checks = criterion.get("checks") or [criterion]
        for check in checks:
            human = check.get("human_review") or {}
            decision = str(human.get("decision") or "")
            status = str(check.get("status") or criterion.get("status") or "").lower()
            label = str(
                check.get("requirement")
                or check.get("criterion_text")
                or check.get("title")
                or criterion.get("requirement")
                or criterion.get("criterion_text")
                or criterion.get("title")
                or criterion.get("category")
                or "Requirement"
            )
            if decision == "meets_requirement":
                bucket = "passed"
            elif decision == "does_not_meet":
                bucket = "failed"
            elif check.get("evaluation_state") == "not_evaluated_after_blocker" or status == "not_evaluated":
                bucket = "not_evaluated"
            elif status in pass_statuses:
                bucket = "passed"
            elif status in fail_statuses:
                bucket = "failed"
            elif status in ask_statuses:
                bucket = "review"
            else:
                bucket = "review"
            counts[bucket] += 1
            if label not in requirements[bucket]:
                requirements[bucket].append(label)
    counts["total"] = sum(counts.values())
    normalized = str(final_decision or "").lower()
    if counts["failed"]:
        outcome = "fail"
        label = "Does not qualify"
    elif counts["review"] and "reject" not in normalized:
        outcome = "review"
        label = "Documents required"
    elif qualifies is True or normalized in {"qualified", "financial_opening_allowed"}:
        outcome = "pass"
        label = "Qualifies"
    elif qualifies is False or "reject" in normalized:
        outcome = "fail"
        label = "Does not qualify"
    else:
        outcome = "review"
        label = "Human review required"
    counts.update(
        {
            "outcome": outcome,
            "label": label,
            "final_decision": final_decision or "",
            "key_passes": requirements["passed"][:5],
            "key_failures": requirements["failed"][:5],
            "key_reviews": requirements["review"][:5],
            "key_asks": requirements["review"][:8],
            "answer": (
                f"{label}: {counts['passed']} of {counts['total']} rules passed; "
                f"{counts['failed']} failed; {counts['review']} need a document or review; "
                f"{counts['not_evaluated']} "
                f"{'was' if counts['not_evaluated'] == 1 else 'were'} not evaluated."
            ),
        }
    )
    return counts


def _normalized_requirement(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _review_lookup_key(value: Any) -> str:
    return str(value or "").removeprefix("BHEL-").casefold()


def _latest_reviews_by_criterion(session: Any, evaluation: Evaluation) -> dict[str, Review]:
    latest: dict[str, Review] = {}
    for review in session.scalars(
        select(Review)
        .where(Review.evaluation_id == evaluation.id)
        .order_by(Review.created_at)
    ):
        data = review.payload_json if isinstance(getattr(review, "payload_json", None), dict) else {}
        for raw in (data.get("criterion_id"), data.get("checkpoint_id")):
            key = _review_lookup_key(raw)
            if key:
                latest[key] = review
    return latest


def _public_row_criterion_id(
    item: dict[str, Any],
    *,
    index: int,
    internal_criteria: list[dict[str, Any]],
    internal_findings: list[dict[str, Any]],
) -> str:
    explicit = str(item.get("criterion_id") or "").strip()
    if explicit:
        return explicit
    requirement = _normalized_requirement(item.get("requirement"))
    if requirement:
        finding_matches = [
            row
            for row in internal_findings
            if _normalized_requirement(row.get("criterion_text") or row.get("requirement")) == requirement
        ]
        if len(finding_matches) == 1:
            found = str(finding_matches[0].get("criterion_id") or "").strip()
            if found:
                return found
        criterion_matches = [
            row
            for row in internal_criteria
            if _normalized_requirement(row.get("requirement")) == requirement
        ]
        if len(criterion_matches) == 1:
            found = str(criterion_matches[0].get("criterion_id") or "").strip()
            if found:
                return found
    number = item.get("no") if isinstance(item.get("no"), int) else index
    if 0 < number <= len(internal_criteria):
        found = str(internal_criteria[number - 1].get("criterion_id") or "").strip()
        if found:
            return found
    return ""


def _human_decision(review: Any) -> str:
    data = review.payload_json if isinstance(getattr(review, "payload_json", None), dict) else {}
    return str(data.get("decision") or getattr(review, "action", "") or "")


def _overlay_human_reviews_on_criteria_results(
    criteria_results: list[Any],
    *,
    internal_criteria: list[Any],
    internal_findings: list[Any],
    reviews_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    # Public rows expose `no`, not criterion_id. Join through findings / tender.criteria.
    internal_criteria = [row for row in internal_criteria if isinstance(row, dict)]
    internal_findings = [row for row in internal_findings if isinstance(row, dict)]
    overlaid: list[dict[str, Any]] = []
    for index, item in enumerate(criteria_results or [], start=1):
        row = dict(item) if isinstance(item, dict) else {}
        criterion_id = _public_row_criterion_id(
            row,
            index=index,
            internal_criteria=internal_criteria,
            internal_findings=internal_findings,
        )
        review = reviews_by_id.get(_review_lookup_key(criterion_id))
        if review is not None:
            payload = dict(review.payload_json) if isinstance(getattr(review, "payload_json", None), dict) else {}
            decision = _human_decision(review)
            if decision == "meets_requirement":
                row["status"] = "passed"
                row["result"] = "Passed"
            elif decision == "does_not_meet":
                row["status"] = "failed"
                row["result"] = "Failed"
            elif decision:
                row["status"] = "needs_manual_review"
                row["result"] = "Needs manual review"
            row["human_review"] = payload
            if payload.get("corrected_value"):
                row["observed_value"] = payload["corrected_value"]
            reason = str(payload.get("reason") or getattr(review, "reason", "") or "")
            if reason:
                row["why"] = reason
        overlaid.append(row)
    return overlaid


_FAIL_FINDING_STATUSES = {
    "fail",
    "failed",
    "fail_fact",
    "present_but_invalid",
    "missing",
    "missing_after_complete_search",
    "unclear",
    "needs_review",
    "needs_manual_review",
    "not_evaluated",
}


def _overlay_human_reviews_on_findings(
    findings: list[Any],
    reviews_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    overlaid: list[dict[str, Any]] = []
    for finding in findings or []:
        item = dict(finding) if isinstance(finding, dict) else {}
        review = reviews_by_id.get(_review_lookup_key(item.get("criterion_id")))
        if review is not None:
            payload = dict(review.payload_json) if isinstance(getattr(review, "payload_json", None), dict) else {}
            decision = _human_decision(review)
            if decision == "meets_requirement":
                item["status"] = "pass"
            elif decision == "does_not_meet":
                item["status"] = "fail"
            elif decision:
                item["status"] = "unclear"
            item["human_review"] = payload
            reason = str(payload.get("reason") or getattr(review, "reason", "") or "")
            if reason:
                item["reason"] = reason
        overlaid.append(item)
    return overlaid


def _findings_block_qualification(findings: list[dict[str, Any]]) -> bool:
    return any(
        bool(item.get("mandatory", True))
        and str(item.get("status") or "").lower() in _FAIL_FINDING_STATUSES
        for item in findings
    )


def _apply_human_reviews(session: Any, evaluation: Evaluation, payload: dict[str, Any]) -> None:
    latest_pair: dict[tuple[str, str], Review] = {}
    latest_id: dict[str, Review] = {}
    for review in session.scalars(
        select(Review)
        .where(Review.evaluation_id == evaluation.id)
        .order_by(Review.created_at)
    ):
        data = review.payload_json or {}
        criterion_id = str(data.get("criterion_id") or "")
        checkpoint_id = str(data.get("checkpoint_id") or criterion_id)
        if criterion_id:
            latest_pair[(criterion_id, checkpoint_id)] = review
            latest_id[criterion_id] = review
    for criterion in payload.get("criteria") or []:
        for check in criterion.get("checks") or []:
            criterion_id = str(check.get("criterion_id") or "")
            key = (
                criterion_id,
                str(check.get("checkpoint_id") or criterion_id),
            )
            review = latest_pair.get(key) or latest_id.get(criterion_id)
            if review:
                check["human_review"] = review.payload_json
