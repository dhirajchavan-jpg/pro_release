"""LLM-backed document verification for bidder evidence."""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from dataclasses import dataclass
from typing import Any

from ai_provider import AIProviderOutputError

from procurement.domain.schemas import (
    DocumentVerification,
    DocumentVerificationSet,
    CriteriaEvidenceMap,
    EvidenceMapEntry,
    EvidencePacketSet,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
    RequirementMatrix,
    RequirementMatrixItem,
    SpecialistFindingSet,
)
from procurement.domain.serialization import model_dump, validate_model
from procurement.domain.text import clean_text, normalize_for_match, unique_items
from procurement.evaluation.assessment import page_text_index
from procurement.evaluation.criteria import criterion_is_prohibition, criterion_retrieval_policy
from procurement.evaluation.evidence_pipeline import (
    EVIDENCE_PACKET_MAX_PAGES,
    EVIDENCE_VERIFIER_PAGE_CHARS,
    apply_specialist_findings_to_verification,
    criterion_page_excerpt,
    logical_document_text_index,
    page_source_index,
    verification_needs_recovery,
)
from procurement.evaluation.evidence_scout import (
    exhaustive_recovery_candidate_pages,
    evidence_vision_media,
    full_corpus_search_completed,
    recovery_candidate_pages,
)
from procurement.evaluation.ocr_recovery import (
    snippets_for_evidence_entry,
    snippets_for_evidence_packet,
)
from procurement.evaluation.semantic_retrieval import (
    EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND,
    content_unique_pages,
    page_content_key,
)
from procurement.evaluation.validation import (
    evidence_proof_contract_gap,
    grounded_verification_page_ids,
    objective_verification_reason,
)
from procurement.evaluation.facts import bidder_identifier_evidence_ledger
from procurement.evaluation.validators import financial_check_coverage_pages
from procurement.evaluation.verification import apply_deterministic_verification
from procurement.evaluation.visual_verification import (
    apply_visual_signature_checks,
    visual_signature_agent,
)
from procurement.platform.job_context import JOB_CONTEXT, check_job_deadline
from procurement.platform.llm import llm_json
from procurement.platform.prompts import prompt_text
from procurement.understanding.classification import tender_source_trap
from procurement.understanding.quality import MAX_DOC_CHARS


logger = logging.getLogger("procurement_evaluator")

EVIDENCE_RECOVERY_MAX_ROUNDS = max(0, int(os.getenv("EVIDENCE_RECOVERY_MAX_ROUNDS", "0")))
EVIDENCE_RECOVERY_MAX_SECONDS = max(1, int(os.getenv("EVIDENCE_RECOVERY_MAX_SECONDS", "45")))
VERIFICATION_CONCURRENCY = max(1, int(os.getenv("VERIFICATION_CONCURRENCY", "4")))
EVIDENCE_VERIFICATION_BATCH_SIZE = max(
    1, int(os.getenv("EVIDENCE_VERIFICATION_BATCH_SIZE", "8"))
)
EVIDENCE_VERIFICATION_BATCH_MAX_IMAGES = max(
    1, int(os.getenv("EVIDENCE_VERIFICATION_BATCH_MAX_IMAGES", "16"))
)


@dataclass
class _PreparedEvidence:
    entry: EvidenceMapEntry
    item: RequirementMatrixItem
    evidence: EvidenceRequirement
    policy: dict[str, Any]
    snippets: list[str]
    snippet_page_ids: list[str]
    inspected_page_ids: list[str]
    media_parts: list[dict[str, Any]]
    visual_checks: list[dict[str, Any]]


def document_verifier_agent(
    matrix: RequirementMatrix,
    evidence_map: CriteriaEvidenceMap,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
    evidence_packets: EvidencePacketSet | None = None,
    specialist_findings: SpecialistFindingSet | None = None,
    semantic_index: dict[str, Any] | None = None,
) -> DocumentVerificationSet:
    matrix_by_id = {item.criterion_id: item for item in matrix.items}
    doc_text_by_logical_id = logical_document_text_index(logical_documents, docs)
    page_text_by_id = page_text_index(docs)
    page_source_by_id = page_source_index(docs)
    recovery_pages = content_unique_pages([
        page
        for document in docs
        if document.record.upload_side == "vendor"
        for page in document.pages
        if page.text.strip()
    ])
    logical_ids_by_page: dict[str, list[str]] = {}
    logical_by_id = {
        document.logical_document_id: document
        for document in logical_documents
        if document.upload_side == "vendor"
    }
    for document in logical_documents:
        if document.upload_side != "vendor":
            continue
        for page_id in document.page_refs:
            logical_ids_by_page.setdefault(page_id, []).append(document.logical_document_id)
    worker_deadline = getattr(JOB_CONTEXT, "deadline", None)
    worker_job_id = getattr(JOB_CONTEXT, "job_id", None)
    worker_usage_log = getattr(JOB_CONTEXT, "llm_usage", None)
    identifier_ledger = bidder_identifier_evidence_ledger(docs)
    visual_inspection_cache: dict[str, Future[dict[str, Any]]] = {}
    visual_inspection_lock = Lock()

    def prepare_entry(entry: EvidenceMapEntry) -> _PreparedEvidence | None:
        item = matrix_by_id.get(entry.criterion_id)
        if not item:
            return None
        evidence = next(
            (
                value
                for value in item.required_evidence
                if value.evidence_id == entry.evidence_id
            ),
            EvidenceRequirement(
                evidence_id=entry.evidence_id,
                document_name=entry.document_name,
                document_type=entry.document_type,
                checks=entry.checks,
            ),
        )
        policy = next(
            (
                criterion_retrieval_policy(item, candidate)
                for candidate in item.required_evidence
                if candidate.evidence_id == entry.evidence_id
            ),
            {"final_pages": EVIDENCE_PACKET_MAX_PAGES},
        )
        coverage_pages = financial_check_coverage_pages(item, evidence, recovery_pages)
        coverage_page_ids = [page.page_id for page in coverage_pages]
        if coverage_page_ids:
            entry.matched_page_ids = unique_items(coverage_page_ids + entry.matched_page_ids)
            entry.matched_logical_document_ids = unique_items(
                entry.matched_logical_document_ids
                + [
                    logical_id
                    for page_id in coverage_page_ids
                    for logical_id in logical_ids_by_page.get(page_id, [])
                ]
            )
        packet_page_ids = [
            page_id
            for packet in (evidence_packets.packets if evidence_packets else [])
            if packet.criterion_id == entry.criterion_id and packet.evidence_id == entry.evidence_id
            for page_id in packet.page_ids
        ]
        packet_snippets = snippets_for_evidence_packet(
            entry,
            evidence_packets,
            max_pages=int(policy["final_pages"]),
        )
        if packet_snippets:
            snippets = packet_snippets
            snippet_page_ids = packet_page_ids[:len(snippets)]
        else:
            snippets = snippets_for_evidence_entry(entry, page_text_by_id, doc_text_by_logical_id)
            snippet_page_ids = entry.matched_page_ids[:len(snippets)]
        if coverage_pages:
            existing_pairs = list(zip(snippet_page_ids, snippets))
            coverage_pairs = [
                (page.page_id, criterion_page_excerpt(page, item, evidence))
                for page in coverage_pages
            ]
            coverage_ids = set(coverage_page_ids)
            combined = coverage_pairs + [
                pair for pair in existing_pairs if pair[0] not in coverage_ids
            ]
            combined = combined[:max(1, int(policy["final_pages"]))]
            snippet_page_ids = [page_id for page_id, _snippet in combined]
            snippets = [snippet for _page_id, snippet in combined]
        inspected_page_ids = unique_items(entry.matched_page_ids + packet_page_ids)
        media_parts, visual_checks = evidence_vision_media(entry, page_source_by_id, inspected_page_ids)
        return _PreparedEvidence(
            entry=entry,
            item=item,
            evidence=evidence,
            policy=dict(policy),
            snippets=snippets,
            snippet_page_ids=snippet_page_ids,
            inspected_page_ids=inspected_page_ids,
            media_parts=media_parts,
            visual_checks=visual_checks,
        )

    prepared_entries = [
        prepared
        for entry in evidence_map.entries
        if (prepared := prepare_entry(entry)) is not None
    ]
    initial_results: dict[tuple[str, str], DocumentVerification] = {}

    def run_initial_batch(batch: list[_PreparedEvidence]) -> list[DocumentVerification]:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        def verify_individually(prepared: _PreparedEvidence) -> DocumentVerification:
            try:
                return verify_evidence_with_gemini(
                    prepared.item,
                    prepared.entry,
                    prepared.snippets,
                    media_parts=prepared.media_parts,
                    snippet_page_ids=prepared.snippet_page_ids,
                )
            except AIProviderOutputError as exc:
                logger.warning(
                    "Individual criterion verifier output rejected criterion_id=%s evidence_id=%s category=%s",
                    prepared.entry.criterion_id,
                    prepared.entry.evidence_id,
                    exc.category,
                )
                return DocumentVerification(
                    criterion_id=prepared.entry.criterion_id,
                    evidence_id=prepared.entry.evidence_id,
                    logical_document_ids=prepared.entry.matched_logical_document_ids,
                    status="unclear",
                    check_results={
                        check: "unclear"
                        for check in (prepared.entry.checks or ["presence"])
                    },
                    reason=(
                        "The submitted evidence could not be verified reliably from the provider response; "
                        "human review is required."
                    ),
                )

        if len(batch) == 1:
            return [verify_individually(batch[0])]
        try:
            results = verify_evidence_batch_with_gemini(batch)
            returned = {
                (result.criterion_id, result.evidence_id)
                for result in results
            }
            missing = [
                prepared
                for prepared in batch
                if (prepared.entry.criterion_id, prepared.entry.evidence_id) not in returned
            ]
            if missing:
                logger.warning(
                    "Batched criterion verifier omitted %s of %s results; retrying only omitted items",
                    len(missing),
                    len(batch),
                )
                results.extend(
                    verify_individually(prepared)
                    for prepared in missing
                )
            return results
        except AIProviderOutputError as exc:
            logger.warning(
                "Batched criterion verifier output rejected size=%s category=%s reason=%s; falling back to individual verification",
                len(batch),
                exc.category,
                exc,
            )
            return [verify_individually(prepared) for prepared in batch]

    payable_entries = [
        prepared
        for prepared in prepared_entries
        if prepared.entry.matched_logical_document_ids or prepared.entry.matched_page_ids
    ]
    batches = verification_batches(payable_entries)
    if VERIFICATION_CONCURRENCY == 1 or len(batches) <= 1:
        initial_batches = [run_initial_batch(batch) for batch in batches]
    else:
        with ThreadPoolExecutor(max_workers=min(VERIFICATION_CONCURRENCY, len(batches))) as executor:
            initial_batches = list(executor.map(run_initial_batch, batches))
    for results in initial_batches:
        for result in results:
            initial_results[(result.criterion_id, result.evidence_id)] = result

    def verify_entry(prepared: _PreparedEvidence) -> DocumentVerification:
        JOB_CONTEXT.deadline = worker_deadline
        JOB_CONTEXT.job_id = worker_job_id
        JOB_CONTEXT.llm_usage = worker_usage_log
        entry = prepared.entry
        item = prepared.item
        evidence = prepared.evidence
        policy = prepared.policy
        snippets = prepared.snippets
        snippet_page_ids = prepared.snippet_page_ids
        inspected_page_ids = prepared.inspected_page_ids
        media_parts = prepared.media_parts
        visual_checks = prepared.visual_checks
        visually_judged = any(check.get("status") == "inspected" for check in visual_checks)
        initial_verification = initial_results.get((entry.criterion_id, entry.evidence_id))
        initial_used = False

        def verify(
            snippet_values: list[str],
            media_values: list[dict[str, Any]],
            snippet_ids: list[str],
        ) -> DocumentVerification:
            nonlocal initial_used
            try:
                if initial_verification is not None and not initial_used:
                    result = initial_verification
                    initial_used = True
                else:
                    result = verify_evidence_with_gemini(
                        item,
                        entry,
                        snippet_values,
                        media_parts=media_values,
                        snippet_page_ids=snippet_ids,
                    )
                apply_deterministic_verification(
                    result,
                    item,
                    entry,
                    snippet_values,
                    identifier_ledger=identifier_ledger,
                    selected_page_ids=snippet_ids,
                )
                return result
            except AIProviderOutputError as exc:
                logger.warning(
                    "Criterion verifier output rejected criterion_id=%s evidence_id=%s category=%s",
                    entry.criterion_id,
                    entry.evidence_id,
                    exc.category,
                )
                return DocumentVerification(
                    criterion_id=entry.criterion_id,
                    evidence_id=entry.evidence_id,
                    logical_document_ids=entry.matched_logical_document_ids,
                    status="unclear",
                    check_results={check: "unclear" for check in (entry.checks or ["presence"])},
                    reason="The evidence verifier returned an incomplete structured response; human review is required.",
                )

        if entry.matched_logical_document_ids or entry.matched_page_ids:
            verification = verify(snippets, media_parts, snippet_page_ids)
        else:
            verification = DocumentVerification(
                criterion_id=entry.criterion_id,
                evidence_id=entry.evidence_id,
                status="missing",
                check_results={check: "missing" for check in entry.checks},
                reason=f"Required document missing: {entry.document_name}",
            )
        verification.visual_checks.extend(visual_checks)

        # Clean native text stays text-first.  If the verifier is still unsure,
        # inspect the already-shortlisted pages visually before widening search.
        if (
            verification.status in {"missing", "unclear"}
            and not visually_judged
            and inspected_page_ids
        ):
            uncertainty_media, uncertainty_checks = evidence_vision_media(
                entry,
                page_source_by_id,
                inspected_page_ids,
                force_reason="uncertain_text_verification",
            )
            if uncertainty_media:
                verification = verify(snippets, uncertainty_media, snippet_page_ids)
                verification.visual_checks.extend(visual_checks + uncertainty_checks)
                media_parts = uncertainty_media
                visual_checks.extend(uncertainty_checks)
                visually_judged = True

        recovery_started = time.perf_counter()
        recovery_round = 0
        seen_page_ids = set(inspected_page_ids)
        initially_seen_content = {
            page_content_key(page_source_by_id[page_id][1])
            for page_id in inspected_page_ids
            if page_id in page_source_by_id
        }
        seen_page_ids.update(
            page.page_id
            for page in recovery_pages
            if page_content_key(page) in initially_seen_content
        )
        remaining_signal_pages = exhaustive_recovery_candidate_pages(
            item,
            evidence,
            recovery_pages,
            seen_page_ids,
            max_pages=len(recovery_pages) or 1,
        )
        corpus_search_complete = full_corpus_search_completed(
            entry,
            recovery_pages,
            semantic_index,
        )
        recovery_exhausted = bool(corpus_search_complete and not remaining_signal_pages)
        while (
            verification_needs_recovery(verification)
            and not recovery_exhausted
            and recovery_round < EVIDENCE_RECOVERY_MAX_ROUNDS
            and time.perf_counter() - recovery_started < EVIDENCE_RECOVERY_MAX_SECONDS
        ):
            check_job_deadline()
            recovered = (
                remaining_signal_pages[:EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND]
                if corpus_search_complete
                else recovery_candidate_pages(
                    item,
                    evidence,
                    recovery_pages,
                    seen_page_ids,
                    semantic_index,
                )
            )
            if not recovered:
                break
            recovery_round += 1
            recovered_ids = [page.page_id for page in recovered]
            seen_page_ids.update(recovered_ids)
            entry.matched_page_ids = unique_items(entry.matched_page_ids + recovered_ids)
            entry.matched_logical_document_ids = unique_items(
                entry.matched_logical_document_ids
                + [logical_id for page_id in recovered_ids for logical_id in logical_ids_by_page.get(page_id, [])]
            )
            recovery_snippets = [criterion_page_excerpt(page, item, evidence) for page in recovered]
            recovery_media, recovery_visual_checks = evidence_vision_media(
                entry,
                page_source_by_id,
                recovered_ids,
            )
            max_snippets = max(1, MAX_DOC_CHARS // EVIDENCE_VERIFIER_PAGE_CHARS)
            snippets = (snippets + recovery_snippets)[-max_snippets:]
            snippet_page_ids = (snippet_page_ids + recovered_ids)[-max_snippets:]
            verification = verify(snippets, recovery_media or media_parts, snippet_page_ids)
            verification.visual_checks.extend(visual_checks + recovery_visual_checks)
            if recovery_media:
                media_parts = recovery_media
            visual_checks.extend(recovery_visual_checks)
            visually_judged = visually_judged or any(
                check.get("status") == "inspected" for check in recovery_visual_checks
            )
            if corpus_search_complete:
                remaining_signal_pages = exhaustive_recovery_candidate_pages(
                    item,
                    evidence,
                    recovery_pages,
                    seen_page_ids,
                    max_pages=len(recovery_pages) or 1,
                )
                recovery_exhausted = not remaining_signal_pages
            logger.info(
                "Criterion recovery criterion_id=%s evidence_id=%s round=%s pages=%s status=%s",
                entry.criterion_id,
                entry.evidence_id,
                recovery_round,
                len(recovered_ids),
                verification.status,
            )
        if EVIDENCE_RECOVERY_MAX_ROUNDS > 0 and verification_needs_recovery(verification):
            max_snippets = max(1, MAX_DOC_CHARS // EVIDENCE_VERIFIER_PAGE_CHARS)
            residual_pages = exhaustive_recovery_candidate_pages(
                item,
                evidence,
                recovery_pages,
                seen_page_ids,
                max_pages=min(
                    max_snippets,
                    max(int(policy["final_pages"]), EVIDENCE_RECOVERY_MAX_PAGES_PER_ROUND),
                ),
            )
            if residual_pages:
                check_job_deadline()
                residual_ids = [page.page_id for page in residual_pages]
                seen_page_ids.update(residual_ids)
                entry.matched_page_ids = unique_items(entry.matched_page_ids + residual_ids)
                entry.matched_logical_document_ids = unique_items(
                    entry.matched_logical_document_ids
                    + [
                        logical_id
                        for page_id in residual_ids
                        for logical_id in logical_ids_by_page.get(page_id, [])
                    ]
                )
                combined_pairs = list(zip(snippet_page_ids, snippets)) + [
                    (page.page_id, criterion_page_excerpt(page, item, evidence))
                    for page in residual_pages
                ]
                unique_pairs: list[tuple[str, str]] = []
                pair_ids: set[str] = set()
                for page_id, snippet in combined_pairs:
                    if page_id and page_id not in pair_ids:
                        unique_pairs.append((page_id, snippet))
                        pair_ids.add(page_id)
                if len(unique_pairs) > max_snippets:
                    anchor_count = max(1, max_snippets // 2)
                    unique_pairs = unique_pairs[:anchor_count] + unique_pairs[-(max_snippets - anchor_count):]
                snippet_page_ids = [page_id for page_id, _snippet in unique_pairs]
                snippets = [snippet for _page_id, snippet in unique_pairs]
                residual_media, residual_visual_checks = evidence_vision_media(
                    entry,
                    page_source_by_id,
                    residual_ids,
                )
                verification = verify(snippets, residual_media or media_parts, snippet_page_ids)
                verification.visual_checks.extend(visual_checks + residual_visual_checks)
                if residual_media:
                    media_parts = residual_media
                visual_checks.extend(residual_visual_checks)
                visually_judged = visually_judged or any(
                    check.get("status") == "inspected" for check in residual_visual_checks
                )
                logger.info(
                    "Criterion exhaustive recovery criterion_id=%s evidence_id=%s pages=%s status=%s",
                    entry.criterion_id,
                    entry.evidence_id,
                    len(residual_ids),
                    verification.status,
                )
        remaining_signal_pages = exhaustive_recovery_candidate_pages(
            item,
            evidence,
            recovery_pages,
            seen_page_ids,
            max_pages=len(recovery_pages) or 1,
        )
        corpus_search_complete = full_corpus_search_completed(
            entry,
            recovery_pages,
            semantic_index,
        )
        entry.search_coverage.update(
            {
                "verification_pages_inspected": len(seen_page_ids),
                "verification_search_exhausted": bool(
                    corpus_search_complete and not remaining_signal_pages
                ),
                "remaining_signal_page_ids": [page.page_id for page in remaining_signal_pages],
            }
        )
        if verification.status == "missing":
            if corpus_search_complete and not remaining_signal_pages:
                verification.reason = (
                    "Required evidence was not found after full-corpus semantic and exact-signal search of the readable bidder submission."
                )
            else:
                verification.status = "unclear"
                verification.check_results = {
                    check: "unclear" if status == "missing" else status
                    for check, status in verification.check_results.items()
                }
                verification.reason = (
                    "Evidence was not found in the pages selected for verification, but complete semantic search coverage "
                    "or exact-signal inspection could not be established."
                )
        quote_text = normalize_for_match(verification.quote or "")
        if verification.status == "pass" and (
            tender_source_trap(quote_text)
            or any(
                phrase in quote_text
                for phrase in (
                    "shall be submitted",
                    "should be submitted",
                    "must be submitted",
                    "have to be submitted",
                    "required to submit",
                )
            )
        ):
            verification.status = "unclear"
            verification.reason = "The cited text states a tender instruction; it does not prove bidder compliance."
            verification.check_results["bidder_proof"] = "unclear"
        if not visually_judged:
            apply_visual_signature_checks(verification, entry, snippets)
        apply_deterministic_verification(
            verification,
            item,
            entry,
            snippets,
            identifier_ledger=identifier_ledger,
            selected_page_ids=snippet_page_ids,
        )
        if specialist_findings:
            apply_specialist_findings_to_verification(verification, entry, specialist_findings.findings)
        visual_signature_agent(
            verification,
            entry,
            page_source_by_id,
            inspection_cache=visual_inspection_cache,
            cache_lock=visual_inspection_lock,
        )
        proof_gap = evidence_proof_contract_gap(
            evidence,
            entry,
            logical_by_id,
            verification,
            selected_page_texts=[
                page_text_by_id.get(page_id, "")
                for page_id in verification.page_ids
                if page_id in page_text_by_id
            ],
            criterion=item,
        )
        if verification.status == "pass" and proof_gap:
            verification.status = "unclear"
            verification.check_results["proof_contract"] = "unclear"
            verification.reason = proof_gap
        verification.page_ids = grounded_verification_page_ids(
            verification,
            entry,
            page_text_by_id,
        )
        if verification.status in {"pass", "fail"} and not verification.page_ids:
            verification.status = "unclear"
            verification.check_results["provenance"] = "unclear"
            verification.reason = "The proposed decision could not be tied to an exact submitted page or inspected image."
        verification.reason = objective_verification_reason(verification, entry)
        return verification

    if VERIFICATION_CONCURRENCY == 1 or len(prepared_entries) <= 1:
        results = [verify_entry(prepared) for prepared in prepared_entries]
    else:
        with ThreadPoolExecutor(max_workers=min(VERIFICATION_CONCURRENCY, len(prepared_entries))) as executor:
            futures = [executor.submit(verify_entry, prepared) for prepared in prepared_entries]
            results = [future.result() for future in futures]

    return DocumentVerificationSet(items=results)


def verification_batches(entries: list[_PreparedEvidence]) -> list[list[_PreparedEvidence]]:
    """Batch overlapping evidence so each bidder page image is sent as few times as possible."""
    remaining = list(entries)
    batches: list[list[_PreparedEvidence]] = []
    while remaining:
        batch = [remaining.pop(0)]
        page_ids = set(batch[0].inspected_page_ids)
        while remaining and len(batch) < EVIDENCE_VERIFICATION_BATCH_SIZE:
            candidates = [
                (
                    len(page_ids.intersection(candidate.inspected_page_ids)),
                    -len(page_ids.union(candidate.inspected_page_ids)),
                    index,
                )
                for index, candidate in enumerate(remaining)
                if len(page_ids.union(candidate.inspected_page_ids))
                <= EVIDENCE_VERIFICATION_BATCH_MAX_IMAGES
            ]
            if not candidates:
                break
            _overlap, _size, index = max(candidates)
            candidate = remaining.pop(index)
            batch.append(candidate)
            page_ids.update(candidate.inspected_page_ids)
        batches.append(batch)
    return batches


def _criterion_payload(
    item: RequirementMatrixItem,
    evidence_id: str,
) -> dict[str, Any]:
    payload = model_dump(item)
    # These extensions duplicate the normalized decision fields above and are
    # retained in the audit record; sending them again cannot change the rule.
    payload.pop("extensions", None)
    payload["required_evidence"] = [
        model_dump(evidence)
        for evidence in item.required_evidence
        if evidence.evidence_id == evidence_id
    ]
    return payload


def _evidence_payload(entry: EvidenceMapEntry) -> dict[str, Any]:
    return {
        "criterion_id": entry.criterion_id,
        "evidence_id": entry.evidence_id,
        "document_name": entry.document_name,
        "document_type": entry.document_type,
        "checks": entry.checks,
        "required": entry.required,
        "matched_logical_document_ids": entry.matched_logical_document_ids,
        "matched_page_ids": entry.matched_page_ids,
    }


def _merged_media_parts(entries: list[_PreparedEvidence]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for prepared in entries:
        parts = prepared.media_parts
        for index in range(0, len(parts) - 1, 2):
            label, image = parts[index], parts[index + 1]
            text = str(label.get("text") or "")
            match = re.search(r"page_id=([^,\s]+)", text)
            page_id = match.group(1) if match else text
            if page_id in seen_pages:
                continue
            seen_pages.add(page_id)
            merged.extend([label, image])
    return merged


def _shared_page_contexts(entries: list[_PreparedEvidence]) -> list[dict[str, str]]:
    """Send each shortlisted bidder-page excerpt once per verifier batch."""
    by_page: dict[str, str] = {}
    for prepared in entries:
        for page_id, snippet in zip(prepared.snippet_page_ids, prepared.snippets):
            text = clean_text(snippet)
            if text and len(text) > len(by_page.get(page_id, "")):
                by_page[page_id] = text
    return [{"page_id": page_id, "text": text} for page_id, text in by_page.items()]


def _verification_from_data(
    item: RequirementMatrixItem,
    entry: EvidenceMapEntry,
    snippets: list[str],
    media_parts: list[dict[str, Any]],
    snippet_page_ids: list[str],
    data: dict[str, Any],
) -> DocumentVerification:
    required_checks = list(dict.fromkeys(entry.checks or ["presence"]))
    candidate_page_ids = unique_items(entry.matched_page_ids + snippet_page_ids)
    if data.get("criterion_id") not in {None, "", entry.criterion_id}:
        raise AIProviderOutputError("document verifier returned invalid JSON criterion_id")
    if data.get("evidence_id") not in {None, "", entry.evidence_id}:
        raise AIProviderOutputError("document verifier returned invalid JSON evidence_id")
    data = dict(data)
    data.setdefault("criterion_id", entry.criterion_id)
    data.setdefault("evidence_id", entry.evidence_id)
    data.setdefault("logical_document_ids", entry.matched_logical_document_ids)
    data.setdefault("page_ids", [])
    if data.get("status") in {"pass", "fail"} and not data.get("page_ids"):
        returned_quote = normalize_for_match(str(data.get("quote") or ""))
        if returned_quote and snippet_page_ids:
            data["page_ids"] = [
                page_id
                for page_id, snippet in zip(snippet_page_ids, snippets)
                if returned_quote in normalize_for_match(snippet)
            ]
        if not data.get("page_ids") and media_parts and len(candidate_page_ids) == 1:
            data["page_ids"] = list(candidate_page_ids)
    checks = data.get("check_results")
    if (
        not isinstance(checks, dict)
        or any(check not in checks for check in required_checks)
        or any(value not in {"pass", "fail", "missing", "unclear"} for value in checks.values())
        or not isinstance(data.get("page_ids"), list)
        or any(str(page_id) not in candidate_page_ids for page_id in data.get("page_ids") or [])
        or (data.get("status") in {"pass", "fail"} and not data.get("page_ids"))
        or not clean_text(str(data.get("reason") or ""))
        or (data.get("status") == "pass" and not data.get("quote") and not media_parts)
    ):
        raise AIProviderOutputError("document verifier returned invalid JSON evidence result")
    try:
        verification = validate_model(DocumentVerification, data)
    except Exception as exc:
        raise AIProviderOutputError("document verifier returned invalid JSON schema") from exc
    verification.criterion_id = entry.criterion_id
    verification.evidence_id = entry.evidence_id
    verification.logical_document_ids = entry.matched_logical_document_ids
    if verification.status == "pass" and _has_unapproved_offered_alternative(item, verification.quote):
        verification.status = "unclear"
        if "tender_requirement_match" in verification.check_results:
            verification.check_results["tender_requirement_match"] = "unclear"
        verification.reason = (
            "The bid offers an alternative that the tender makes subject to buyer acceptance, "
            "but the supplied evidence does not confirm that acceptance."
        )
    return verification


def verify_evidence_batch_with_gemini(
    prepared_entries: list[_PreparedEvidence],
) -> list[DocumentVerification]:
    checks = sorted({
        check
        for prepared in prepared_entries
        for check in (prepared.entry.checks or ["presence"])
    })
    media_parts = _merged_media_parts(prepared_entries)
    shared_page_contexts = _shared_page_contexts(prepared_entries)
    data = llm_json(
        prompt_text(
            "document_verifier",
            "You are the strict procurement evidence judge. Verify every requested evidence item independently using the supplied bidder text and page images. Return only JSON.",
        ),
        {
            "task": "verify_document_evidence_batch",
            "rules": [
                "Return exactly one result for every supplied criterion_id and evidence_id pair; never combine items.",
                "Shared bidder page contexts are pre-selected pages. Use only the page_ids referenced by each evidence item and do not use tender instructions inside them as proof of bidder compliance.",
                "Do not assume a document is signed or stamped unless text or a supplied image proves it.",
                "If a required check cannot be verified, mark that check unclear.",
                "A required missing document is missing, not unclear.",
                "Pass only when every required check is supported by an exact quote or directly inspected page image.",
                "Return only candidate page_ids belonging to that evidence item.",
                "Keep each reason to at most two short, objective sentences about that item.",
                "Do not decide the whole bid.",
            ],
            "shared_bidder_page_contexts": shared_page_contexts,
            "evidence_items": [
                {
                    "criterion_mode": (
                        "prohibition" if criterion_is_prohibition(prepared.item) else "positive_requirement"
                    ),
                    "criterion": _criterion_payload(
                        prepared.item, prepared.entry.evidence_id
                    ),
                    "evidence_requirement": _evidence_payload(prepared.entry),
                    "candidate_page_ids": unique_items(
                        prepared.entry.matched_page_ids + prepared.snippet_page_ids
                    ),
                    "snippet_page_ids": prepared.snippet_page_ids,
                    "vendor_document_snippets": prepared.snippets[len(prepared.snippet_page_ids):],
                    "required_checks": list(
                        dict.fromkeys(prepared.entry.checks or ["presence"])
                    ),
                }
                for prepared in prepared_entries
            ],
            "json_shape": {
                "results": [
                    {
                        "criterion_id": "exact supplied criterion_id",
                        "evidence_id": "exact supplied evidence_id",
                        "logical_document_ids": ["submitted logical document id"],
                        "page_ids": ["exact candidate page_id supporting this result"],
                        "status": "pass|fail|missing|unclear",
                        "check_results": {"required_check": "pass|fail|missing|unclear"},
                        "reason": "short evidence-specific reason",
                        "quote": "short supporting quote or empty string",
                    }
                ]
            },
        },
        media_parts=media_parts,
        response_schema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "evidence_id": {"type": "string"},
                            "logical_document_ids": {"type": "array", "items": {"type": "string"}},
                            "page_ids": {"type": "array", "items": {"type": "string"}},
                            "status": {"type": "string", "enum": ["pass", "fail", "missing", "unclear"]},
                            "check_results": {
                                "type": "object",
                                "properties": {
                                    check: {"type": "string", "enum": ["pass", "fail", "missing", "unclear"]}
                                    for check in checks
                                },
                            },
                            "reason": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "required": [
                            "criterion_id", "evidence_id", "logical_document_ids", "page_ids",
                            "status", "check_results", "reason", "quote",
                        ],
                    },
                }
            },
            "required": ["results"],
        },
    )
    results = data.get("results")
    if not isinstance(results, list):
        raise AIProviderOutputError("document verifier returned invalid batched JSON results")
    expected = {
        (prepared.entry.criterion_id, prepared.entry.evidence_id)
        for prepared in prepared_entries
    }
    returned = {
        (str(result.get("criterion_id") or ""), str(result.get("evidence_id") or ""))
        for result in results
        if isinstance(result, dict)
    }
    if not results or len(returned) != len(results) or not returned.issubset(expected):
        raise AIProviderOutputError("document verifier returned invalid batched JSON result identities")
    by_key = {
        (str(result["criterion_id"]), str(result["evidence_id"])): result
        for result in results
    }
    parsed: list[DocumentVerification] = []
    for prepared in prepared_entries:
        key = (prepared.entry.criterion_id, prepared.entry.evidence_id)
        if key not in by_key:
            continue
        try:
            parsed.append(
                _verification_from_data(
                    prepared.item,
                    prepared.entry,
                    prepared.snippets,
                    prepared.media_parts,
                    prepared.snippet_page_ids,
                    by_key[key],
                )
            )
        except AIProviderOutputError as exc:
            logger.warning(
                "Batched criterion verifier result rejected criterion_id=%s evidence_id=%s reason=%s",
                key[0],
                key[1],
                exc,
            )
    if not parsed:
        raise AIProviderOutputError("document verifier returned no valid batched JSON results")
    return parsed


def verify_evidence_with_gemini(
    item: RequirementMatrixItem,
    entry: EvidenceMapEntry,
    snippets: list[str],
    *,
    media_parts: list[dict[str, Any]] | None = None,
    snippet_page_ids: list[str] | None = None,
) -> DocumentVerification:
    required_checks = list(dict.fromkeys(entry.checks or ["presence"]))
    candidate_page_ids = unique_items(entry.matched_page_ids + list(snippet_page_ids or []))
    criterion_payload = _criterion_payload(item, entry.evidence_id)
    check_status_schema = {
        "type": "string",
        "enum": ["pass", "fail", "missing", "unclear"],
    }
    data = llm_json(
        prompt_text(
            "document_verifier",
            "You are the strict procurement evidence judge. Verify the requested evidence using the supplied bidder text and page images. Return only JSON.",
        ),
        {
            "task": "verify_document_evidence",
            "rules": [
                "Evidence snippets are pre-selected pages; do not use tender instructions inside them as proof of bidder compliance.",
                "Do not assume a document is signed or stamped unless the text clearly shows signature, signed, seal, stamp, digitally signed, or equivalent.",
                "If a required check cannot be verified from the provided text, mark that check unclear.",
                "When page images are supplied, inspect them directly and use visible tables, certificates, signatures, stamps, identifiers, dates, and values as primary evidence.",
                "For a visually inspected page, do not reject valid evidence merely because OCR or flattened table text is incomplete.",
                "A required missing document is missing, not unclear.",
                "Return pass only when the requested document type and every required check are supported by an exact quote or a directly inspected page image.",
                "When the tender says an offered alternative requires buyer acceptance or approval, do not pass merely because the bidder offered it. Pass only with explicit acceptance evidence; otherwise use unclear.",
                "For pass, fail, or unclear, return only the exact candidate page_ids that directly support the decision. For missing evidence, return a page only when it contains partial evidence that explains what is still missing.",
                "Use an empty page_ids list only when no relevant bidder page exists.",
                "Start the reason with the objective answer. For an identifier or value, state the exact identifier or value first.",
                "Keep the reason to at most two short sentences. Do not mention Gemini, AI, model judgment, internal pipeline names, proof contracts, artifact labels, retrieval, embeddings, or semantic search. State the actual document, value, field, issuer, signature, date, or condition that passed or remains unresolved.",
                "Keep the reason strictly about this evidence item and criterion; do not assert unrelated schedules, values, or documents.",
                (
                    "For a prohibition criterion, verify the bidder's affirmative legal identity or participation structure. "
                    "Do not require a standalone statement denying the prohibited arrangement unless the tender explicitly requires that declaration. "
                    "Pass only when the evidence identifies one eligible legal entity; fail when it identifies a forbidden JV/consortium; otherwise use unclear."
                    if criterion_is_prohibition(item)
                    else "This is not a prohibition criterion."
                ),
                "Do not decide the whole bid; only verify this evidence item.",
            ],
            "criterion_mode": "prohibition" if criterion_is_prohibition(item) else "positive_requirement",
            "criterion": criterion_payload,
            "evidence_requirement": _evidence_payload(entry),
            "candidate_page_ids": candidate_page_ids,
            "vendor_document_snippets": snippets,
            "snippet_page_ids": list(snippet_page_ids or []),
            "json_shape": {
                "criterion_id": entry.criterion_id,
                "evidence_id": entry.evidence_id,
                "logical_document_ids": entry.matched_logical_document_ids,
                "page_ids": ["exact candidate page_id supporting the decision"],
                "status": "pass|fail|missing|unclear",
                "check_results": {"presence": "pass|fail|missing|unclear"},
                "reason": "short reason",
                "quote": "short supporting quote or empty string",
            },
        },
        media_parts=media_parts,
        response_schema={
            "type": "object",
            "properties": {
                "criterion_id": {"type": "string"},
                "evidence_id": {"type": "string"},
                "logical_document_ids": {"type": "array", "items": {"type": "string"}},
                "page_ids": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": ["pass", "fail", "missing", "unclear"]},
                "check_results": {
                    "type": "object",
                    "properties": {
                        check: check_status_schema for check in required_checks
                    },
                    "required": required_checks,
                },
                "reason": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": [
                "criterion_id",
                "evidence_id",
                "logical_document_ids",
                "page_ids",
                "status",
                "check_results",
                "reason",
                "quote",
            ],
        },
    )
    return _verification_from_data(
        item,
        entry,
        snippets,
        list(media_parts or []),
        list(snippet_page_ids or []),
        data,
    )


def _has_unapproved_offered_alternative(item: RequirementMatrixItem, quote: str | None) -> bool:
    requirement = clean_text(f"{item.requirement} {item.pass_rule} {item.source_quote or ''}")
    normalized_requirement = normalize_for_match(requirement)
    if not any(
        phrase in normalized_requirement
        for phrase in ("acceptable to", "subject to approval", "subject to acceptance", "approved by")
    ):
        return False
    normalized_quote = normalize_for_match(str(quote or ""))
    if any(phrase in normalized_quote for phrase in ("accepted by", "approved by", "approval granted")):
        return False
    alternatives = [
        option
        for pair in re.findall(r"\b([A-Za-z][A-Za-z0-9-]{2,})\s*/\s*([A-Za-z][A-Za-z0-9-]{2,})\b", str(quote or ""))
        for option in pair
    ]
    return bool(alternatives) and any(normalize_for_match(option) not in normalized_requirement for option in alternatives)
