"""Projection of tender source material into stable public tender schemas."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from base_rfp import BaseRfpUnderstanding

from procurement.domain.catalog import PROCUREMENT_SOURCES
from procurement.domain.document_dictionary import (
    procurement_document_dictionary_context,
    procurement_dictionary_entries_for_text,
)
from procurement.domain.schemas import (
    Criterion,
    ExtractedDocument,
    PageUnit,
    TenderEvidenceDocument,
    TenderExtraction,
    TenderProfile,
    TenderRequiredDocument,
    TenderRequirementExtraction,
    TenderSourceRef,
)
from procurement.domain.serialization import document_context, model_dump, validate_model
from procurement.domain.text import clean_text, normalize_for_match
from procurement.evaluation.normalization import normalize_tender_profile_data
from procurement.platform.llm import (
    GEMINI_MODEL_DEEP,
    GEMINI_MODEL_FAST,
    PROCUREMENT_OFFLINE_LLM,
    llm_json,
)
from procurement.platform.prompts import prompt_text
from procurement.understanding.classification import (
    TENDER_LLM_HIGH_SIGNAL_TERMS,
    tender_page_signals,
)
from procurement.understanding.extraction import (
    add_requirement_document,
    apply_requirement_consolidation_result,
    backfill_mandatory_document_criteria,
    backfill_tender_extraction,
    compact_tender_partial,
    compact_tender_partials,
    criterion_from_coverage_data,
    criterion_is_covered,
    merge_required_document,
    merge_tender_dict,
    merge_tender_extraction_dicts,
    normalize_extraction_document,
    normalize_tender_extraction_data,
    tender_clause_candidates,
    tender_requirement_consolidation_data,
    tender_source_documents,
)


logger = logging.getLogger("procurement_evaluator")

TENDER_PAGE_LEDGER_MAX_PAGES = max(1, int(os.getenv("TENDER_PAGE_LEDGER_MAX_PAGES", "350")))
TENDER_PAGE_LEDGER_PAGE_CHARS = max(500, int(os.getenv("TENDER_PAGE_LEDGER_PAGE_CHARS", "3000")))
TENDER_LLM_LEDGER_MAX_PAGES = max(1, int(os.getenv("TENDER_LLM_LEDGER_MAX_PAGES", "140")))
TENDER_EXTRACTION_CHUNK_PAGES = max(1, int(os.getenv("TENDER_EXTRACTION_CHUNK_PAGES", "10")))
TENDER_EXTRACTION_CONCURRENCY = max(1, int(os.getenv("TENDER_EXTRACTION_CONCURRENCY", "3")))
TENDER_EXTRACTION_SYNTHESIS_ENABLED = os.getenv("TENDER_EXTRACTION_SYNTHESIS_ENABLED", "0") == "1"
TENDER_REQUIREMENT_CONSOLIDATION_ENABLED = os.getenv("TENDER_REQUIREMENT_CONSOLIDATION_ENABLED", "0") == "1"


def tender_extraction_from_base_rfp(record: BaseRfpUnderstanding, tender_docs: list[ExtractedDocument]) -> TenderExtraction:
    required_by_id = {item.required_document_id: item for item in record.required_documents}
    requirements = []
    for item in record.requirements:
        documents = [base_rfp_required_document_payload(required_by_id[doc_id]) for doc_id in item.required_document_ids if doc_id in required_by_id]
        for evidence_name in item.evidence:
            normalized = normalize_extraction_document(evidence_name)
            if normalized["canonical_key"] and all(doc["canonical_key"] != normalized["canonical_key"] for doc in documents):
                documents.append(normalized)
        requirements.append(
            TenderRequirementExtraction(
                requirement_id=item.requirement_id,
                requirement=item.text or item.title,
                category=item.category or "other",
                stage=item.stage or "other",
                mandatory=item.mandatory is True,
                failure_effect=item.failure_effect or "manual_review",
                required_documents=[TenderEvidenceDocument(**document) for document in documents],
                extraction_status="extracted" if item.status == "stated" else "unresolved",
                missing_detail="" if item.status == "stated" else item.status,
                source_refs=[base_rfp_tender_source_ref(ref) for ref in item.source_refs],
            )
        )

    required_documents = []
    for item in record.required_documents:
        payload = base_rfp_required_document_payload(item)
        payload.update(
            {
                "required_for_requirement_ids": item.requirement_ids,
                "source_annexures": item.annexure_ids,
                "extraction_status": "extracted" if item.source_refs else "unresolved",
                "mandatory_status": "required" if item.mandatory is True else ("optional" if item.mandatory is False else "conditional"),
            }
        )
        required_documents.append(TenderRequiredDocument(**payload))

    bid_structure_text = normalize_for_match(
        " ".join([record.bid_rules.bid_structure, *record.bid_rules.submission_instructions])
    )
    inferred_cover_keys: list[str] = []
    if any(marker in bid_structure_text for marker in ("price bid", "financial bid", "commercial bid")):
        inferred_cover_keys.append("financial_bid_boq")
    if any(marker in bid_structure_text for marker in ("techno commercial", "technical bid")):
        inferred_cover_keys.append("technical_commercial_compliance")
    if inferred_cover_keys:
        matching_requirement_ids = []
        for requirement in requirements:
            requirement_text = normalize_for_match(requirement.requirement)
            if any(marker in requirement_text for marker in ("two part", "two cover", "three part", "three cover", "price bid", "financial bid")):
                matching_requirement_ids.append(requirement.requirement_id)
                for key in inferred_cover_keys:
                    add_requirement_document(requirement, key)
        required_by_key = {document.canonical_key: document for document in required_documents}
        for key in inferred_cover_keys:
            merge_required_document(
                required_by_key,
                key,
                requirement_ids=matching_requirement_ids,
                extraction_status="referenced_not_available",
                mandatory_status="required",
            )
        required_documents = list(required_by_key.values())

    timeline = {
        event.event_type or event.name or event.event_id: {
            "name": event.name,
            "start": event.start,
            "end": event.end,
            "duration": event.duration,
            "source_refs": [model_dump(base_rfp_tender_source_ref(ref)) for ref in event.source_refs],
        }
        for event in record.schedule.events
    }
    financial_terms = record.financial_terms.model_dump(mode="json")
    policies = {
        normalize_for_match(item.name or item.policy_id).replace(" ", "_"): item.model_dump(mode="json")
        for item in record.policies.items
        if item.name or item.policy_id
    }
    buyer = record.parties.buyer or record.parties.issuer
    tender = {
        "tender_title": record.identity.title,
        "tender_number": record.identity.reference_number,
        "tender_identifiers": [
            {
                "type": "tender_id",
                "value": record.identity.reference_number,
                "status": "extracted" if record.identity.reference_number else "not_present",
                "source_refs": [],
            }
        ],
        "buyer": buyer.name if buyer else "",
        "procurement_type": record.procurement.procurement_type,
        "bid_system": record.procurement.bid_system or record.bid_rules.bid_structure,
        "scope_summary": record.scope.summary,
        "evaluation_method": record.evaluation.method,
        "timeline": timeline,
        "commercial_terms_visible": financial_terms,
        "policy_applicability": policies,
    }
    missing_attachments = [
        {
            "attachment": gap.expected or gap.description,
            "why_needed": gap.description,
            "impact_on_extraction": gap.impact,
        }
        for gap in record.gaps
        if "attachment" in gap.gap_type or "referenced" in gap.gap_type
    ]
    not_extractable = [
        {"field": gap.gap_type, "value": None, "status": gap.status or "unresolved"}
        for gap in record.gaps
        if "attachment" not in gap.gap_type and "referenced" not in gap.gap_type
    ]
    annexures = [
        {
            "annexure_no": item.number,
            "title": item.title,
            "presence_in_uploaded_pdf": item.availability,
            "document_role": item.annexure_type,
            "canonical_document_keys": [required_by_id[doc_id].name for doc_id in item.required_document_ids if doc_id in required_by_id],
            "requires_bidder_submission": item.required_submission,
            "source_ref": model_dump(base_rfp_tender_source_ref(item.source_refs[0])) if item.source_refs else {},
        }
        for item in record.annexures
    ]
    data = {
        "schema_version": "tender_extraction_v1",
        "source_stage": "base_rfp_projection",
        "extraction_status": "complete" if record.coverage.status == "complete" else "insufficient_source",
        "source_documents": tender_source_documents(tender_docs),
        "tender": tender,
        "annexures": annexures,
        "requirements": [model_dump(item) for item in requirements],
        "required_documents": [model_dump(item) for item in required_documents],
        "tender_packet_gaps": {"missing_referenced_attachments": missing_attachments, "not_extractable_fields": not_extractable},
        "audit": {"derived_by": "base_rfp_projection", "base_rfp_record_id": record.record_id, "coverage_status": record.coverage.status},
    }
    normalize_tender_extraction_data(data, tender_docs)
    return backfill_tender_extraction(validate_model(TenderExtraction, data), tender_docs)

def base_rfp_required_document_payload(item: Any) -> dict[str, Any]:
    return normalize_extraction_document(
        {
            "display_name": item.name,
            "category": item.category,
            "stage": item.stage or "bid_stage",
            "checks": item.checks or ["presence"],
        }
    )

def base_rfp_tender_source_ref(ref: Any) -> TenderSourceRef:
    page_id = str(getattr(ref, "page_id", "") or "")
    document_id = page_id.rsplit("-p", 1)[0] if "-p" in page_id else str(getattr(ref, "document", "") or "")
    return TenderSourceRef(
        document_id=document_id,
        page_number=getattr(ref, "page", None),
        page_id=page_id,
        block_id=str(getattr(ref, "block", "") or ""),
        table_id=str(getattr(ref, "table", "") or ""),
        cell_id=str(getattr(ref, "cell", "") or ""),
        quote=str(getattr(ref, "quote", "") or ""),
    )

def tender_page_ledger(documents: list[ExtractedDocument]) -> list[dict[str, Any]]:
    rows: list[tuple[int, int, dict[str, Any]]] = []
    source_index = 0
    for document in documents:
        pages = document.pages or [
            PageUnit(
                page_id=f"{document.record.document_id}-p1",
                document_id=document.record.document_id,
                file_name=document.record.file_name,
                upload_side=document.record.upload_side,
                page_number=1,
                text=document.text,
            )
        ]
        classification = document.record.classification
        for page in pages:
            text = clean_text(page.text or "")
            if not text:
                continue
            dictionary_entries = procurement_dictionary_entries_for_text(text)
            signals = tender_page_signals(text)
            score = len(signals) * 3 + len(dictionary_entries)
            rows.append(
                (
                    source_index,
                    score,
                    {
                        "page_id": page.page_id,
                        "document_id": page.document_id,
                        "file_name": page.file_name,
                        "page_number": page.page_number,
                        "classification": model_dump(classification) if classification else None,
                        "signals": signals[:16],
                        "dictionary_matches": [
                            {
                                "canonical_key": entry.get("canonical_key", ""),
                                "display_name": entry.get("display_name", ""),
                                "stage": entry.get("stage", "bid_stage"),
                                "checks": entry.get("checks", [])[:8],
                            }
                            for entry in dictionary_entries[:16]
                        ],
                        "text_chars": len(text),
                        "text": text[:TENDER_PAGE_LEDGER_PAGE_CHARS],
                    },
                )
            )
            source_index += 1
    if len(rows) <= TENDER_PAGE_LEDGER_MAX_PAGES:
        return [row for _, _, row in rows]
    selected_indexes = {
        index
        for index, _ in sorted(enumerate(rows), key=lambda item: (-item[1][1], item[1][0]))[:TENDER_PAGE_LEDGER_MAX_PAGES]
    }
    return [row for index, (_, _, row) in enumerate(rows) if index in selected_indexes]

def tender_extraction_llm_data(
    tender_docs: list[ExtractedDocument],
    page_ledger: list[dict[str, Any]] | None = None,
    task: str = "extract_tender_extraction",
    include_document_text: bool = True,
) -> dict[str, Any]:
    ledger = page_ledger if page_ledger is not None else tender_page_ledger(tender_docs)
    data = llm_json(
        prompt_text("tender_extraction", "You are a tender extraction agent for Indian government and PSU procurement. Read every supplied tender/RFP/ATC/corrigendum source and return the canonical tender_extraction_v1 JSON. Extract only source-backed facts. Return only JSON."),
        {
            "task": task,
            "procurement_sources": PROCUREMENT_SOURCES,
            "document_dictionary": procurement_document_dictionary_context(),
            "rules": [
                "The tender/RFP packet is the source of truth.",
                "Use page_ledger as the primary source because it contains page-level text from the uploaded tender/RFP/corrigendum packet. Do not decide from file names alone.",
                "Read across all page_ledger rows and all uploaded tender-side files, including corrigendum/addendum/pre-bid clarification/guidelines.",
                "For tender.tender_number, an official GeM Bid Number (GEM/YYYY/B/NNNNNNN) is canonical when present. Keep a different buyer-internal RFP/reference in tender_identifiers; otherwise use the buyer's formal published reference. Never promote a corrigendum number, filename number, download ID, UUID, or portal record ID.",
                "Use document_dictionary canonical_key/display_name/document_type/checks when a required bidder document maps to a known procurement document.",
                "Extract one atomic requirement per independent bid-stage pass/fail/manual-review/price-impact decision.",
                "If the tender lists annexures/forms/declarations one by one, extract one annexures[] item for each listed annexure; never collapse I-XV or multiple annexures into one row.",
                "For each listed annexure that is a bidder declaration, form, confirmation, upload instruction, BQC/PQC instruction, technical compliance, price/BOQ instruction, or policy declaration, add a required_documents[] entry using the closest dictionary key.",
                "If a listed annexure creates an evaluation condition but the detailed annexure body is missing, still add a requirement with extraction_status referenced_not_available and explain missing_detail.",
                "For BQC/PQC, technical specification, BOQ/price schedule, PPP-MII/local content, land-border, integrity pact, deviation statement, site visit, social media, billing, and TPIA confirmations, do not skip them just because details are missing; mark the details as referenced_not_available.",
                "Do not invent BQC, EMD, turnover, similar-work, BOQ, technical, or financial thresholds when the uploaded packet only references a missing annexure.",
                "When an annexure/detail file is referenced but absent, record it in tender_packet_gaps and mark affected requirements/documents as referenced_not_available or unresolved.",
                "Post-award obligations are not bid-stage missing documents unless the tender explicitly asks for a bid-stage undertaking/proof.",
                "Keep source_refs with document_id, page_number, and a short quote for every requirement.",
                "Do not include raw page text in output fields. Keep every source_refs[].quote under 160 characters.",
                "For tender.scope_summary, cover the procured object and major workstreams only; do not include delivery deadlines, payment milestones, eligibility thresholds, or scoring marks.",
                "For chunk tasks, extract only facts visible in this page_ledger chunk; leave arrays empty when the chunk has no bid-stage/evaluation/submission fact.",
                "Do not turn generic contract administration clauses into evaluation criteria unless they affect bid submission, eligibility, technical scoring, commercial/financial opening, preference, or an explicitly required undertaking/document.",
                "A table of contents or annexure index can create annexures and required_documents, but not requirements unless an actual clause/table says the bidder must, shall, is eligible, is scored, is rejected, or is blocked from financial opening.",
                "Do not create a requirement only because an annexure title exists.",
                "Never enumerate individual product features, functional specification bullets, service level rows, payment milestones, or boilerplate contract duties as separate requirements. Return one section-level requirement for them when they affect bid evaluation.",
                "For one chunk, return at most 5 requirements and 12 required_documents. Prefer source-backed gates over generic terms.",
            ],
            "documents": document_context(tender_docs) if include_document_text else tender_source_documents(tender_docs),
            "page_ledger": ledger,
            "json_shape": {
                "schema_version": "tender_extraction_v1",
                "dataset_case_id": None,
                "source_stage": "runtime",
                "extraction_status": "complete|partial_referenced_attachments_missing|insufficient_source",
                "source_documents": [{"document_id": "tender-1", "file_name": "file.pdf", "document_type": "rfp_or_bid_document", "page_count": 1, "extraction_status": "ok", "content_scope": "short scope"}],
                "tender": {
                    "tender_title": "string",
                    "tender_number": "string",
                    "tender_identifiers": [{"type": "tender_id|bid_no|rfp_no|crfq_no|other", "value": "string", "status": "extracted|not_present", "source_refs": [{"document_id": "tender-1", "page_number": 1, "quote": "short quote"}]}],
                    "buyer": "string",
                    "procurement_type": "goods|services|works|consultancy|unknown",
                    "bid_system": "single bid|two bid|two stage|GeM|unknown",
                    "scope_summary": "source-backed object plus major workstreams, no deadlines",
                    "timeline": {},
                    "commercial_terms_visible": {},
                    "policy_applicability": {},
                },
                "annexures": [{"annexure_no": "I", "title": "string", "presence_in_uploaded_pdf": "present|listed_only|referenced_not_available", "document_role": "tender_source_attachment|bidder_submission_form|instruction", "canonical_document_keys": ["dictionary key"], "requires_bidder_submission": "yes|no|unresolved", "source_ref": {"document_id": "tender-1", "page_number": 1, "quote": "short quote"}}],
                "requirements": [
                    {
                        "requirement_id": "T1-C1",
                        "requirement": "source-backed requirement",
                        "category": "submission|eligibility|technical|commercial|financial|compliance|preference_policy|other",
                        "stage": "bid_stage|responsiveness|pqc|technical|commercial|financial|preference|contract|other",
                        "mandatory": True,
                        "failure_effect": "reject_unresponsive|reject_technical|block_financial_opening|affects_price_ranking|manual_review|no_direct_fail",
                        "required_documents": [{"canonical_key": "dictionary key", "display_name": "document display name", "document_type": "dictionary evidence_type", "category": "dictionary category", "stage": "dictionary stage", "checks": ["presence"]}],
                        "extraction_status": "extracted|referenced_not_available|not_present_in_uploaded_document|unresolved",
                        "missing_detail": "why detail is incomplete, if any",
                        "source_refs": [{"document_id": "tender-1", "page_number": 1, "quote": "short quote"}],
                    }
                ],
                "required_documents": [{"canonical_key": "dictionary key", "display_name": "document display name", "document_type": "dictionary evidence_type", "category": "dictionary category", "stage": "bid_stage", "checks": ["presence"], "required_for_requirement_ids": ["T1-C1"], "source_annexures": [], "extraction_status": "extracted|referenced_not_available|unresolved", "mandatory_status": "required|optional|conditional|unresolved"}],
                "tender_packet_gaps": {"missing_referenced_attachments": [{"attachment": "Annexure name", "why_needed": "why needed", "impact_on_extraction": "what cannot be decided"}], "not_extractable_fields": [{"field": "field_name", "value": None, "status": "not_present_in_uploaded_document|referenced_not_available"}]},
                "audit": {"derived_by": "tender_extraction_agent"},
            },
        },
    )
    return data

def tender_extraction_agent(tender_docs: list[ExtractedDocument]) -> TenderExtraction:
    ledger = tender_page_ledger(tender_docs)
    llm_ledger = tender_llm_ledger(ledger)
    data = (
        chunked_tender_extraction_data(tender_docs, llm_ledger)
        if len(llm_ledger) > TENDER_EXTRACTION_CHUNK_PAGES
        else tender_extraction_llm_data(tender_docs, llm_ledger)
    )
    normalize_tender_extraction_data(data, tender_docs)
    return backfill_tender_extraction(validate_model(TenderExtraction, data), tender_docs)

def tender_llm_ledger(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[tuple[int, int, dict[str, Any]]] = []
    for index, row in enumerate(ledger):
        signals = set(row.get("signals") or [])
        dictionary_keys = {str(item.get("canonical_key") or "") for item in row.get("dictionary_matches") or [] if isinstance(item, dict)}
        high_signal = bool(signals & TENDER_LLM_HIGH_SIGNAL_TERMS)
        non_generic_document_hit = bool(dictionary_keys - {"technical_spec_compliance", "methodology_work_plan"})
        if index < 5 or high_signal or non_generic_document_hit:
            score = len(signals & TENDER_LLM_HIGH_SIGNAL_TERMS) * 4 + len(dictionary_keys) + (5 if index < 5 else 0)
            selected.append((index, score, row))
    if not selected:
        return ledger[:TENDER_EXTRACTION_CHUNK_PAGES]
    if len(selected) > TENDER_LLM_LEDGER_MAX_PAGES:
        keep_indexes = {
            index
            for index, _, _ in sorted(selected, key=lambda item: (-item[1], item[0]))[:TENDER_LLM_LEDGER_MAX_PAGES]
        }
        selected = [item for item in selected if item[0] in keep_indexes]
    return [row for _, _, row in selected]

def chunked_tender_extraction_data(tender_docs: list[ExtractedDocument], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    chunks = [ledger[index : index + TENDER_EXTRACTION_CHUNK_PAGES] for index in range(0, len(ledger), TENDER_EXTRACTION_CHUNK_PAGES)]
    partials: list[dict[str, Any]] = []

    def run_chunk(index_and_chunk: tuple[int, list[dict[str, Any]]]) -> dict[str, Any]:
        index, chunk = index_and_chunk
        try:
            return tender_extraction_llm_data(
                tender_docs,
                chunk,
                task=f"extract_tender_extraction_chunk_{index + 1}",
                include_document_text=False,
            )
        except Exception as exc:
            logger.warning("Tender extraction chunk %s failed: %s", index + 1, exc)
            return {
                "schema_version": "tender_extraction_v1",
                "source_stage": "runtime",
                "extraction_status": "insufficient_source",
                "source_documents": tender_source_documents(tender_docs),
                "tender": {},
                "annexures": [],
                "requirements": [],
                "required_documents": [],
                "tender_packet_gaps": {
                    "missing_referenced_attachments": [],
                    "not_extractable_fields": [{"field": f"chunk_{index + 1}", "value": None, "status": "llm_chunk_failed"}],
                },
                "audit": {"derived_by": "tender_extraction_chunk_agent", "error": str(exc)[:300]},
            }

    with ThreadPoolExecutor(max_workers=min(TENDER_EXTRACTION_CONCURRENCY, len(chunks))) as executor:
        partials = list(executor.map(run_chunk, enumerate(chunks)))

    merged = merge_tender_extraction_dicts(partials, tender_docs)
    if TENDER_REQUIREMENT_CONSOLIDATION_ENABLED:
        try:
            apply_requirement_consolidation_result(merged, tender_requirement_consolidation_data(tender_docs, merged))
        except Exception as exc:
            logger.warning("Tender requirement consolidation failed: %s", exc)
    try:
        merge_tender_dict(merged["tender"], tender_identity_summary_data(tender_docs, ledger).get("tender", {}))
    except Exception as exc:
        logger.warning("Tender identity/scope extraction failed: %s", exc)
    if not TENDER_EXTRACTION_SYNTHESIS_ENABLED:
        return merged
    try:
        synthesized = llm_json(
            prompt_text("tender_extraction_synthesis", "You consolidate partial Indian tender extraction JSON from page chunk specialists. Return one canonical tender_extraction_v1 JSON. Extract only source-backed facts. Return only JSON."),
            {
                "task": "consolidate_tender_extraction_chunks",
                "procurement_sources": PROCUREMENT_SOURCES,
                "document_dictionary": procurement_document_dictionary_context(),
                "rules": [
                    "Use only the supplied partial_extractions. Do not invent tender clauses, thresholds, documents, or gaps.",
                    "Deduplicate repeated annexures, requirements, required documents, and packet gaps.",
                    "Preserve distinct requirements when they represent different evaluation decisions, even if the wording is similar.",
                    "Use document_dictionary canonical keys for required_documents where possible.",
                    "Keep source_refs[].quote under 160 characters and never include raw page text.",
                ],
                "partial_extractions": compact_tender_partials(partials),
                "merged_fallback": compact_tender_partial(merged),
                "json_shape": {
                    "schema_version": "tender_extraction_v1",
                    "source_stage": "runtime",
                    "extraction_status": "complete|partial_referenced_attachments_missing|insufficient_source",
                    "source_documents": tender_source_documents(tender_docs),
                    "tender": {},
                    "annexures": [],
                    "requirements": [],
                    "required_documents": [],
                    "tender_packet_gaps": {"missing_referenced_attachments": [], "not_extractable_fields": []},
                    "audit": {"derived_by": "tender_extraction_synthesis_agent"},
                },
            },
            model=GEMINI_MODEL_DEEP,
        )
        return synthesized
    except Exception as exc:
        logger.warning("Tender extraction synthesis failed; using deterministic merge: %s", exc)
        return merged

def tender_identity_summary_data(tender_docs: list[ExtractedDocument], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    identity_rows = []
    seen: set[str] = set()
    for row in ledger[:8]:
        marker = str(row.get("page_id"))
        if marker not in seen:
            identity_rows.append(row)
            seen.add(marker)
    for row in ledger:
        row_text = normalize_for_match(" ".join([str(row.get("text", "")), " ".join(row.get("signals", []))]))
        if any(term in row_text for term in ["scope of work", "request for proposal", "notice inviting tender", "rfp", "tender no", "bid no"]):
            marker = str(row.get("page_id"))
            if marker not in seen:
                identity_rows.append(row)
                seen.add(marker)
        if len(identity_rows) >= 24:
            break
    return llm_json(
        prompt_text("tender_identity_summary", "You extract only tender identity and scope metadata from Indian tender/RFP pages. Return only JSON."),
        {
            "task": "extract_tender_identity_scope",
            "rules": [
                "Return only source-backed tender metadata. Do not extract criteria, required documents, or gaps.",
                "Prefer the main RFP/NIT title and scope over corrigendum boilerplate.",
                "Keep scope_summary to one source-backed sentence covering the procured object and major workstreams; include implementation, integration, support/FM, training, security/DR, functional compliance, licensing, or supply/service streams only when visible in the pages.",
                "Do not put delivery deadlines, payment milestones, eligibility thresholds, or evaluation marks in scope_summary.",
            ],
            "source_documents": tender_source_documents(tender_docs),
            "page_ledger": identity_rows,
            "json_shape": {
                "tender": {
                    "tender_title": "string",
                    "tender_number": "string",
                    "buyer": "string",
                    "procurement_type": "goods|services|works|consultancy|unknown",
                    "bid_system": "single bid|two bid|two stage|GeM|unknown",
                    "scope_summary": "source-backed object plus major workstreams, no deadlines",
                    "timeline": {},
                    "commercial_terms_visible": {},
                    "policy_applicability": {},
                }
            },
        },
        model=GEMINI_MODEL_FAST,
    )

def extract_tender_profile(tender_docs: list[ExtractedDocument]) -> TenderProfile:
    data = llm_json(
        prompt_text("rfp_reader", "You are a tender analyst for Indian government and PSU procurement. Build the effective tender from RFP, ATC, corrigendum/addendum, and pre-bid clarification text. Extract only requirements supported by source text. Return only JSON."),
        {
            "task": "extract_tender_profile",
            "procurement_sources": PROCUREMENT_SOURCES,
            "document_dictionary": procurement_document_dictionary_context(),
            "strict_rules": [
                "Use the tender/RFP as the source of truth.",
                "When a tender clause names a required document, map it to the closest document_dictionary display_name instead of inventing a new synonym.",
                "Apply this precedence where documents conflict: latest corrigendum/addendum, official pre-bid clarification, buyer-added ATC/special conditions, PQC/commercial documents, main GeM/RFP bid, then generic GTC/GPC.",
                "For tender_number, use the official GeM Bid Number (GEM/YYYY/B/NNNNNNN) when a GeM bid document is present; retain any different buyer-internal RFP/reference in source notes. Without GeM, use the buyer's formal published reference. Corrigendum numbers and file/download/portal IDs are never the tender number.",
                "Extract an atomic requirement index: one criterion per independent pass/fail/missing/unclear/scored/price-impact decision.",
                "Cover mandatory documents, PQC eligibility, technical specifications, commercial terms, financial evaluation, preference policies, and clarification rules.",
                "Treat EMD, eligibility, signed forms, bid validity, required certificates, and any explicitly mandatory document as mandatory.",
                "Mark post-award PBG/ePBG/performance security/contract execution obligations as stage contract or post-award, not bid-stage missing documents.",
                "Skip boilerplate GTC/GPC/contract clauses unless bidder evidence, acceptance, price ranking, responsiveness, or financial opening depends on them.",
                "Merge duplicate clauses and keep the strongest source quote.",
                "If the tender packet appears to reference missing ATC/PQC/specification/corrigendum attachments, record that in source_notes.",
                "Do not invent organization-specific rules absent from the tender.",
            ],
            "documents": document_context(tender_docs),
            "json_shape": {
                "tender_title": "string",
                "tender_number": "tender number, bid number, RFP number, NIT number, GeM bid number, or empty string",
                "buyer": "string",
                "procurement_type": "goods/services/works/consultancy/unknown",
                "bid_system": "single envelope/two bid/two stage/GeM/custom/unknown",
                "scope": "short scope",
                "evaluation_method": "L1/QCBS/technical pass-fail/unknown",
                "mandatory_documents": ["document names"],
                "criteria": [
                    {
                        "criterion_id": "C1",
                        "category": "eligibility|technical|commercial|financial|compliance|other",
                        "requirement": "requirement text",
                        "stage": "responsiveness|pqc|technical|commercial|financial|clarification|preference|contract|other",
                        "requirement_type": "mandatory_document|pqc_threshold|technical_spec|commercial_term|financial_rule|declaration|preference_policy|clarifiable_rule|submission_format|other",
                        "failure_effect": "reject_unresponsive|reject_technical|block_financial_opening|affects_price_ranking|manual_review|no_direct_fail",
                        "mandatory": True,
                        "clarifiable": False,
                        "applies_when": "short condition, empty if universal",
                        "evaluation_scope": "bidder|manufacturer|product|item|lot|consortium|bid",
                        "threshold": {"kind": "amount|percentage|years|count|date|score|text|none", "value": "value or null", "unit": "INR|percent|years|months|days|documents|none"},
                        "pass_condition": "how to decide pass",
                        "required_documents": ["documents needed as evidence"],
                        "source_document_id": "tender document id",
                        "source_page_id": "tender page id if known",
                        "source_page_number": "page number if known",
                        "source_clause_no": "clause number if visible",
                        "source_quote": "short quote from tender",
                        "priority": "gate|score|price|evidence|note",
                        "confidence": "0 to 1",
                    }
                ],
                "source_notes": ["important tender notes"],
            },
        },
    )
    normalize_tender_profile_data(data)
    return validate_model(TenderProfile, data)

def criteria_coverage_auditor_agent(tender_docs: list[ExtractedDocument], tender: TenderProfile) -> None:
    candidates = tender_clause_candidates(tender_docs)
    added = 0
    if candidates and not PROCUREMENT_OFFLINE_LLM:
        try:
            data = llm_json(
                prompt_text("criteria_coverage_auditor", "You are a tender criteria coverage auditor. Compare source-quoted clause candidates with extracted criteria. Return only missing atomic bid-evaluation criteria supported by the candidate quotes."),
                {
                    "task": "audit_tender_criteria_coverage",
                    "rules": [
                        "Do not duplicate existing criteria.",
                        "Add only bid-stage evaluation, responsiveness, PQC, technical, commercial, financial, declaration, or preference criteria.",
                        "Do not add post-award PBG/ePBG/contract execution obligations as mandatory bid-stage criteria.",
                        "Every missing criterion must quote one supplied clause candidate.",
                    ],
                    "existing_criteria": [model_dump(criterion) for criterion in tender.criteria],
                    "clause_candidates": candidates[:40],
                    "json_shape": {"missing_criteria": [model_dump(Criterion(criterion_id="C1", category="other", requirement="criterion"))]},
                },
            )
            missing = data.get("missing_criteria") if isinstance(data, dict) else []
            if isinstance(missing, list):
                for item in missing[:20]:
                    if not isinstance(item, dict):
                        continue
                    criterion = criterion_from_coverage_data(item, tender)
                    if criterion and not criterion_is_covered(criterion.requirement, tender.criteria):
                        tender.criteria.append(criterion)
                        added += 1
        except Exception as exc:
            tender.source_notes.append(f"Criteria coverage audit skipped: {exc}")
    added += backfill_mandatory_document_criteria(tender)
    if added:
        tender.source_notes.append(f"Criteria coverage auditor added {added} missing criterion/criteria.")

