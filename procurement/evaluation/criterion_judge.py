"""LLM-backed criterion judgment for evaluated bidder evidence."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from procurement.domain.schemas import (
    CriteriaFinding,
    DocumentVerification,
    DocumentVerificationSet,
    EvidencePacket,
    EvidencePacketSet,
    ExtractedDocument,
    LogicalDocument,
    RequirementMatrix,
    RequirementMatrixItem,
)
from procurement.domain.serialization import model_dump
from procurement.domain.text import normalize_for_match
from procurement.evaluation.assessment import evaluate_evidence_groups, requirement_evidence_groups
from procurement.evaluation.criteria import (
    criterion_allows_alternative_route,
    criterion_is_conditionally_worded,
    criterion_is_prohibition,
)
from procurement.evaluation.validation import page_id_is_cited, provider_neutral_reason
from procurement.platform.job_context import JOB_CONTEXT
from procurement.platform.llm import PROCUREMENT_OFFLINE_LLM, llm_json
from procurement.platform.prompts import prompt_text


GEMINI_CRITERION_JUDGE_ENABLED = os.getenv("GEMINI_CRITERION_JUDGE_ENABLED", "1") != "0"
CRITERION_JUDGE_BATCH_SIZE = max(1, int(os.getenv("CRITERION_JUDGE_BATCH_SIZE", "12")))
VERIFICATION_CONCURRENCY = max(1, int(os.getenv("VERIFICATION_CONCURRENCY", "4")))
logger = logging.getLogger("procurement_evaluator")


def gemini_criterion_judge_agent(
    matrix: RequirementMatrix,
    verification_set: DocumentVerificationSet,
    evidence_packets: EvidencePacketSet,
    findings: list[CriteriaFinding],
    logical_documents: list[LogicalDocument] | None = None,
    docs: list[ExtractedDocument] | None = None,
    vendor_profile: dict | None = None,
) -> list[CriteriaFinding]:
    if not GEMINI_CRITERION_JUDGE_ENABLED or PROCUREMENT_OFFLINE_LLM:
        return findings
    verifications_by_criterion: dict[str, list[DocumentVerification]] = {}
    for verification in verification_set.items:
        verifications_by_criterion.setdefault(verification.criterion_id, []).append(verification)
    packets_by_criterion: dict[str, list[EvidencePacket]] = {}
    for packet in evidence_packets.packets:
        packets_by_criterion.setdefault(packet.criterion_id, []).append(packet)
    finding_by_id = {finding.criterion_id: finding for finding in findings}
    judge_items = [
        item
        for item in matrix.items
        if finding_by_id[item.criterion_id].status == "unclear"
        or str((item.applicability or {}).get("state") or "") == "conditional"
        or criterion_allows_alternative_route(item)
        or criterion_is_prohibition(item)
    ]
    if not judge_items:
        return findings
    known_page_ids = {
        page.page_id
        for document in docs or []
        for page in document.pages
        if page.page_id
    }
    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)

    inventory = [
        {
            "logical_document_id": document.logical_document_id,
            "title": document.title,
            "document_type": document.document_type,
            "content_summary": document.content_summary,
            "key_facts": document.key_facts,
        }
        for document in logical_documents or []
        if document.upload_side == "vendor"
    ]
    inventory_by_id = {
        item["logical_document_id"]: item
        for item in inventory
    }

    def criterion_payload(item: RequirementMatrixItem) -> dict:
        payload = model_dump(item)
        # Extensions duplicate the normalized decision contract and remain in
        # the persisted audit record; the judge only needs the contract itself.
        payload.pop("extensions", None)
        if criterion_is_conditionally_worded(item) and payload.get("applicability", {}).get("state") == "applicable":
            payload["applicability"] = {
                "state": "conditional",
                "rule": item.source_quote or item.requirement,
            }
        return payload

    def judge_batch(items: list[RequirementMatrixItem]) -> list[tuple[str, str, str, str, str, list[str]]]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        data = llm_json(
            prompt_text(
                "criterion_judge",
                "You are the final procurement criterion judge for an Indian procurement bid. Decide each supplied criterion independently from its tender rule and verified bidder evidence. Return only JSON.",
            ),
            {
                "task": "criterion_judge_batch",
                "rules": [
                    "Return one result for every supplied criterion_id and never combine criteria.",
                    "Judge whether this bidder passes each exact tender criterion; do not judge the whole bid.",
                    "Apply the stated pass rule, mandatory effect, and valid alternative or exemption routes.",
                    "For a conditional criterion, first decide its applicability trigger from bidder evidence. Use not_applicable only when the trigger is affirmatively disproved; an unknown trigger is unclear, never missing or not_applicable.",
                    "Return applicability_status and applicability_reason separately from the criterion decision. For an unconditional criterion use applicable. For a conditional criterion, use applicable only when bidder evidence proves its trigger, not_applicable only when bidder evidence disproves it, and unclear otherwise.",
                    "Treat direct page-image verification as primary when OCR or flattened table text is incomplete.",
                    "Pass only when bidder evidence proves the criterion. Use missing when required proof is absent, fail when evidence contradicts the requirement, and unclear when evidence is genuinely ambiguous.",
                    "For numeric thresholds such as years, counts, percentages, or amounts, pass only when cited bidder evidence explicitly proves a value meeting the threshold; words such as established, reputable, experienced, or compliant are not numeric proof.",
                    "When the tender says an offered alternative requires buyer acceptance or approval, do not pass merely because the bidder offered it. Pass only with explicit acceptance evidence; otherwise use unclear.",
                    "Do not treat copied tender instructions as bidder proof.",
                    "When naming a page, use only a page_id listed in passed_evidence_page_ids for that criterion.",
                    "Give a concrete reason naming the evidence that passed, failed, or was missing.",
                    "Keep the reason strictly about the current criterion. Do not assert schedules, values, or documents belonging to another criterion.",
                    "Start with the objective answer. For an identifier, date, amount, count, percentage, or threshold, state the exact verified value before the short explanation.",
                    "Do not mention Gemini, AI, model judgment, prompts, internal pipeline names, proof contracts, artifact labels, retrieval, embeddings, or semantic search. Explain the actual document, value, field, issuer, signature, date, threshold, or applicability condition.",
                    "Do not put internal page IDs in prose. Return page IDs only in the dedicated page-id arrays.",
                ],
                "criteria": [
                    {
                        "criterion_mode": (
                            "prohibition" if criterion_is_prohibition(item) else "positive_requirement"
                        ),
                        "criterion_rule": (
                            "Decide whether the forbidden arrangement is present. A proven single legal entity "
                            "may pass without a separate negative declaration unless the tender explicitly demands "
                            "one. Fail when the bid shows a forbidden JV/consortium; use unclear when participation "
                            "structure is ambiguous."
                            if criterion_is_prohibition(item)
                            else "Decide this positive requirement from its verified evidence."
                        ),
                        "criterion": criterion_payload(item),
                        "server_evidence_groups": requirement_evidence_groups(item),
                        "evidence_verifications": [
                            model_dump(value)
                            for value in verifications_by_criterion.get(item.criterion_id, [])
                        ],
                        "evidence_packets": [
                            {
                                "evidence_id": packet.evidence_id,
                                "document_type": packet.document_type,
                                "page_ids": packet.page_ids,
                                "text": packet.text[:2500],
                            }
                            for packet in packets_by_criterion.get(item.criterion_id, [])
                        ],
                        "baseline_contract_rollup": {
                            "status": finding_by_id[item.criterion_id].status,
                            "reason": finding_by_id[item.criterion_id].reason,
                        },
                        "passed_evidence_page_ids": sorted({
                            page_id
                            for verification in verifications_by_criterion.get(item.criterion_id, [])
                            if verification.status == "pass"
                            for page_id in verification.page_ids
                        }),
                    }
                    for item in items
                ],
                "bidder_profile": vendor_profile or {},
                "bid_document_inventory": [
                    inventory_by_id[document_id]
                    for document_id in sorted({
                        document_id
                        for item in items
                        for verification in verifications_by_criterion.get(item.criterion_id, [])
                        for document_id in verification.logical_document_ids
                        if document_id in inventory_by_id
                    })
                ],
                "json_shape": {
                    "results": [{
                        "criterion_id": "exact supplied criterion_id",
                        "status": "pass|fail|missing|unclear|not_applicable",
                        "reason": "specific evidence-backed reason",
                        "evidence_page_ids": ["page_id selected only from passed_evidence_page_ids"],
                        "applicability_status": "applicable|not_applicable|unclear",
                        "applicability_reason": "bidder-specific explanation of whether the tender trigger applies",
                        "applicability_evidence_page_ids": ["page_id used to decide the trigger"],
                    }]
                },
            },
            response_schema={
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "criterion_id": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pass", "fail", "missing", "unclear", "not_applicable"],
                                },
                                "reason": {"type": "string"},
                                "evidence_page_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "applicability_status": {
                                    "type": "string",
                                    "enum": ["applicable", "not_applicable", "unclear"],
                                },
                                "applicability_reason": {"type": "string"},
                                "applicability_evidence_page_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": [
                                "criterion_id", "status", "reason", "applicability_status",
                                "applicability_reason", "applicability_evidence_page_ids",
                            ],
                        },
                    }
                },
                "required": ["results"],
            },
        )
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raw_results = [data] if data.get("criterion_id") else []
        expected_ids = [item.criterion_id for item in items]
        returned_ids = [
            str(result.get("criterion_id") or "")
            for result in raw_results
            if isinstance(result, dict)
        ]
        valid_ids = {
            criterion_id
            for criterion_id in returned_ids
            if criterion_id in expected_ids and returned_ids.count(criterion_id) == 1
        }
        if not valid_ids:
            raise RuntimeError("criterion judge returned invalid JSON results")
        response_by_id = {
            str(result["criterion_id"]): result
            for result in raw_results
            if isinstance(result, dict)
            and str(result.get("criterion_id") or "") in valid_ids
        }
        judged: list[tuple[str, str, str, str, str, list[str]]] = []
        for item in items:
            if item.criterion_id not in response_by_id:
                continue
            baseline = finding_by_id[item.criterion_id]
            verifications = verifications_by_criterion.get(item.criterion_id, [])
            result = response_by_id[item.criterion_id]
            status = str(result.get("status") or "").strip().lower()
            if status not in {"pass", "fail", "missing", "unclear", "not_applicable"}:
                raise RuntimeError("criterion judge returned invalid JSON status")
            reason = provider_neutral_reason(result.get("reason"))
            if not reason:
                raise RuntimeError("criterion judge returned invalid JSON reason")
            if len(reason) > 400:
                reason = f"{reason[:397].rstrip()}..."
            allowed_page_ids = {
                page_id
                for verification in verifications
                if verification.status == "pass"
                for page_id in verification.page_ids
            }
            selected_page_ids = {
                str(page_id).strip()
                for page_id in result.get("evidence_page_ids") or []
                if str(page_id).strip()
            }
            cited_page_ids = {
                page_id
                for page_id in known_page_ids
                if page_id_is_cited(reason, page_id)
            }
            unsupported_page_ids = (selected_page_ids | cited_page_ids) - allowed_page_ids
            if status == "pass" and unsupported_page_ids:
                status = "unclear"
                reason = (
                    "The proposed pass relied on page references outside the criterion's verified "
                    "evidence set; review the bidder evidence for this criterion."
                )
            if status == "pass" and any(
                phrase in normalize_for_match(reason)
                for phrase in [
                    "does not contain",
                    "do not contain",
                    "not provided",
                    "not present",
                    "could not verify",
                    "cannot verify",
                    "cannot confirm",
                    "insufficient evidence",
                ]
            ):
                status = "unclear"
                reason = f"{reason} The stated pass conflicts with its own evidence explanation."
            if status == "pass" and baseline.status != "pass":
                has_passed_evidence = any(verification.status == "pass" for verification in verifications)
                if not (criterion_allows_alternative_route(item) and has_passed_evidence):
                    status = baseline.status
                    reason = (
                        f"{baseline.reason} A pass was proposed, but the required-evidence gate was not satisfied."
                    )
            if status == "missing" and baseline.status == "unclear" and not any(
                verification.status == "missing" for verification in verifications
            ):
                status = "unclear"
                reason = baseline.reason
            evidence_by_id = {evidence.evidence_id: evidence for evidence in item.required_evidence}
            evidence_groups = evaluate_evidence_groups(
                requirement_evidence_groups(item), verifications, evidence_by_id
            )
            if (
                status in {"missing", "unclear"}
                and evidence_groups
                and all(group["status"] == "pass" for group in evidence_groups)
            ):
                status = "pass"
                reason = baseline.reason
            applicability = str((item.applicability or {}).get("state") or "applicable")
            if applicability == "applicable" and criterion_is_conditionally_worded(item):
                applicability = "conditional"
            applicability_status = str(result.get("applicability_status") or "").strip().lower()
            applicability_reason = provider_neutral_reason(result.get("applicability_reason"))
            applicability_page_ids = [
                str(page_id).strip()
                for page_id in result.get("applicability_evidence_page_ids") or []
                if str(page_id).strip()
            ]
            inspected_page_ids = {
                page_id
                for verification in verifications
                for page_id in verification.page_ids
            } | {
                page_id
                for packet in packets_by_criterion.get(item.criterion_id, [])
                for page_id in packet.page_ids
            }
            if applicability != "conditional":
                applicability_status = "not_applicable" if applicability == "not_applicable" else "applicable"
                applicability_reason = (
                    "The tender marks this requirement as not applicable."
                    if applicability == "not_applicable"
                    else "The tender applies this requirement to every bidder."
                )
                applicability_page_ids = []
            elif applicability_status not in {"applicable", "not_applicable", "unclear"} or not applicability_reason:
                applicability_status = "unclear"
                applicability_reason = "The bidder-specific applicability trigger was not resolved from the submitted evidence."
                applicability_page_ids = []
            elif set(applicability_page_ids) - inspected_page_ids:
                applicability_status = "unclear"
                applicability_reason = "The proposed applicability decision cited pages outside the evidence inspected for this criterion."
                applicability_page_ids = []

            if applicability == "not_applicable":
                status = "not_applicable"
                reason = applicability_reason
            elif applicability == "conditional" and applicability_status == "not_applicable":
                status = "not_applicable"
                reason = applicability_reason
            elif applicability == "conditional" and applicability_status == "unclear" and status != "pass":
                status = "unclear"
                reason = applicability_reason
            elif status == "not_applicable" and applicability_status != "not_applicable":
                status = "unclear"
                reason = "The bidder-specific non-applicability decision is unsupported because the tender criterion is not conditional."
            judged.append((
                item.criterion_id,
                status,
                reason,
                applicability_status,
                applicability_reason,
                applicability_page_ids,
            ))
        return judged

    batches = [
        judge_items[index:index + CRITERION_JUDGE_BATCH_SIZE]
        for index in range(0, len(judge_items), CRITERION_JUDGE_BATCH_SIZE)
    ]
    def complete_batch(items: list[RequirementMatrixItem]) -> list[tuple[str, str, str, str, str, list[str]]]:
        results = judge_batch(items)
        returned = {result[0] for result in results}
        missing = [item for item in items if item.criterion_id not in returned]
        if missing:
            logger.warning(
                "Criterion judge omitted %s of %s results; retrying only omitted criteria",
                len(missing),
                len(items),
            )
            results.extend(result for item in missing for result in judge_batch([item]))
        return results

    if VERIFICATION_CONCURRENCY == 1 or len(batches) <= 1:
        judged = [result for batch in batches for result in complete_batch(batch)]
    else:
        with ThreadPoolExecutor(max_workers=min(VERIFICATION_CONCURRENCY, len(batches))) as executor:
            judged = [
                result
                for future in [executor.submit(complete_batch, batch) for batch in batches]
                for result in future.result()
            ]
    for criterion_id, status, reason, applicability_status, applicability_reason, applicability_page_ids in judged:
        finding = finding_by_id[criterion_id]
        item = next(item for item in matrix.items if item.criterion_id == criterion_id)
        verification_by_evidence = {
            verification.evidence_id: verification
            for verification in verifications_by_criterion.get(criterion_id, [])
        }
        finding.status = status  # type: ignore[assignment]
        finding.reason = reason
        finding.applicability_status = applicability_status  # type: ignore[assignment]
        finding.applicability_reason = applicability_reason
        finding.applicability_page_ids = applicability_page_ids
        if status in {"pass", "not_applicable"}:
            finding.missing_documents = []
        elif status == "missing":
            finding.missing_documents = [
                evidence.document_name
                for evidence in item.required_evidence
                if evidence.required
                and verification_by_evidence.get(evidence.evidence_id)
                and verification_by_evidence[evidence.evidence_id].status == "missing"
            ]
            if not finding.missing_documents:
                finding.status = "unclear"
                finding.reason = "Required evidence is unresolved, but no specific document was proven absent."
    return findings
