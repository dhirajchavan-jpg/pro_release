"""Evidence packet and ledger construction.

Packet construction binds criteria to source pages; ledger construction keeps
the vendor corpus searchable and auditable.  The compatibility hooks are
private implementation details used by the historical facade.
"""

from __future__ import annotations

import os
from typing import Any

from procurement.domain.schemas import (
    CriteriaEvidenceMap,
    DocumentClassification,
    EvidenceLedger,
    EvidenceLedgerChunk,
    EvidencePacket,
    EvidencePacketSet,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    RequirementMatrix,
)
from procurement.domain.text import clean_text
from procurement.evaluation.criteria import criterion_retrieval_policy
from procurement.evaluation.facts import extract_packet_facts
from procurement.understanding.quality import MAX_DOC_CHARS

from .evidence_pipeline_routing import _hook
from .evidence_pipeline_specialists import specialist_type_for_evidence


EVIDENCE_PACKET_NEIGHBOR_PAGES = int(os.getenv("EVIDENCE_PACKET_NEIGHBOR_PAGES", "1"))
EVIDENCE_PACKET_MAX_PAGES = int(os.getenv("EVIDENCE_PACKET_MAX_PAGES", "12"))
EVIDENCE_LEDGER_CHUNK_PAGES = max(1, int(os.getenv("EVIDENCE_LEDGER_CHUNK_PAGES", "5")))


def page_source_index(docs: list[ExtractedDocument]) -> dict[str, tuple[ExtractedDocument, PageUnit]]:
    return {
        page.page_id: (document, page)
        for document in docs
        for page in document.pages
    }


def evidence_packet_builder_agent(
    matrix: RequirementMatrix,
    evidence_map: CriteriaEvidenceMap,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
    *,
    compatibility_hooks: dict[str, Any] | None = None,
    max_pages_default: int = EVIDENCE_PACKET_MAX_PAGES,
) -> EvidencePacketSet:
    matrix_by_id = {item.criterion_id: item for item in matrix.items}
    logical_by_id = {doc.logical_document_id: doc for doc in logical_documents}
    page_indexer = _hook(compatibility_hooks, "page_source_index", page_source_index)
    expand_pages = _hook(compatibility_hooks, "expand_evidence_page_ids", expand_evidence_page_ids)
    specialist_type = _hook(compatibility_hooks, "specialist_type_for_evidence", specialist_type_for_evidence)
    page_sources = page_indexer(docs)
    pages_by_document: dict[str, list[PageUnit]] = {}
    for document in docs:
        pages_by_document[document.record.document_id] = sorted(document.pages, key=lambda page: page.page_number)

    packets: list[EvidencePacket] = []
    for index, entry in enumerate(evidence_map.entries, start=1):
        item = matrix_by_id.get(entry.criterion_id)
        evidence = next(
            (
                value
                for value in (item.required_evidence if item else [])
                if value.evidence_id == entry.evidence_id
            ),
            None,
        )
        max_pages = (
            int(criterion_retrieval_policy(item, evidence)["final_pages"])
            if item and evidence
            else max_pages_default
        )
        seed_page_ids = list(entry.matched_page_ids)
        if not seed_page_ids:
            for logical_id in entry.matched_logical_document_ids:
                seed_page_ids.extend(logical_by_id.get(logical_id, LogicalDocument(logical_document_id=logical_id, upload_side="vendor", title="")).page_refs)
        page_ids = expand_pages(
            seed_page_ids,
            page_sources,
            pages_by_document,
            max_pages=max_pages,
        )
        text_parts = []
        warnings = []
        for page_id in page_ids:
            source = page_sources.get(page_id)
            if not source:
                continue
            _, page = source
            if page.text:
                text_parts.append(f"[page {page.page_number}]\n{page.text}")
            elif page.ocr_required:
                warnings.append(f"{page_id}: OCR required")
        text = clean_text("\n\n".join(text_parts))[:MAX_DOC_CHARS]
        if not page_ids:
            warnings.append("No matched vendor pages for this evidence requirement.")
        packets.append(
            EvidencePacket(
                packet_id=f"EP-{index}",
                criterion_id=entry.criterion_id,
                evidence_id=entry.evidence_id,
                specialist=specialist_type(entry),
                document_name=entry.document_name,
                document_type=entry.document_type,
                logical_document_ids=entry.matched_logical_document_ids,
                page_ids=page_ids,
                text=text,
                extracted_facts=extract_packet_facts(text, document_type=entry.document_type),
                warnings=warnings,
            )
        )
    return EvidencePacketSet(packets=packets)


def expand_evidence_page_ids(
    seed_page_ids: list[str],
    page_sources: dict[str, tuple[ExtractedDocument, PageUnit]],
    pages_by_document: dict[str, list[PageUnit]],
    *,
    max_pages: int = EVIDENCE_PACKET_MAX_PAGES,
    neighbor_pages: int = EVIDENCE_PACKET_NEIGHBOR_PAGES,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for page_id in seed_page_ids:
        if page_id in page_sources and page_id not in seen:
            seen.add(page_id)
            ordered.append(page_id)
            if len(ordered) >= max_pages:
                return ordered
    for page_id in seed_page_ids:
        source = page_sources.get(page_id)
        if not source:
            continue
        _, seed = source
        for page in pages_by_document.get(seed.document_id, []):
            if abs(page.page_number - seed.page_number) > neighbor_pages:
                continue
            if page.page_id in seen:
                continue
            seen.add(page.page_id)
            ordered.append(page.page_id)
            if len(ordered) >= max_pages:
                break
        if len(ordered) >= max_pages:
            break
    return ordered


def evidence_ledger_builder(
    docs: list[ExtractedDocument],
    evidence_packets: EvidencePacketSet | None = None,
    chunk_pages: int = EVIDENCE_LEDGER_CHUNK_PAGES,
    *,
    compatibility_hooks: dict[str, Any] | None = None,
) -> EvidenceLedger:
    criteria_by_page: dict[str, set[str]] = {}
    for packet in (evidence_packets.packets if evidence_packets else []):
        for page_id in packet.page_ids:
            criteria_by_page.setdefault(page_id, set()).add(packet.criterion_id)

    first_classification = _hook(compatibility_hooks, "first_page_classification", first_page_classification)
    chunks: list[EvidenceLedgerChunk] = []
    vendor_documents = 0
    vendor_pages = 0
    weak_pages = 0
    for document in docs:
        if document.record.upload_side != "vendor":
            continue
        pages = sorted(document.pages, key=lambda page: page.page_number)
        if not pages:
            continue
        vendor_documents += 1
        vendor_pages += len(pages)
        for offset in range(0, len(pages), chunk_pages):
            group = pages[offset : offset + chunk_pages]
            text = clean_text("\n\n".join(page.text for page in group if page.text))
            weak_page_ids = [page.page_id for page in group if page.ocr_required or not page.text.strip()]
            weak_pages += len(weak_page_ids)
            classification = first_classification(group) or document.record.classification
            matched_criteria = sorted(
                {
                    criterion_id
                    for page in group
                    for criterion_id in criteria_by_page.get(page.page_id, set())
                }
            )
            chunk_facts = extract_packet_facts(text)
            for page in group:
                page_classification = page.classification or document.record.classification
                page_document_type = page_classification.document_type if page_classification else ""
                for key, value in extract_packet_facts(
                    page.text,
                    document_type=page_document_type,
                ).items():
                    chunk_facts.setdefault(key, value)
            chunks.append(
                EvidenceLedgerChunk(
                    chunk_id=f"EL-{len(chunks) + 1}",
                    document_id=document.record.document_id,
                    file_name=document.record.file_name,
                    upload_side=document.record.upload_side,
                    page_start=group[0].page_number,
                    page_end=group[-1].page_number,
                    page_ids=[page.page_id for page in group],
                    text_chars=sum(page.text_chars for page in group),
                    weak_page_ids=weak_page_ids,
                    extraction_methods=sorted({page.extraction_method for page in group}),
                    document_type=classification.document_type if classification else "unknown",
                    content_role=classification.content_role if classification else "unknown",
                    facts=chunk_facts,
                    matched_criteria=matched_criteria,
                    warnings=sorted({warning for page in group for warning in page.warnings}),
                )
            )
    return EvidenceLedger(
        chunks=chunks,
        coverage_summary={
            "vendor_documents": vendor_documents,
            "vendor_pages": vendor_pages,
            "chunks": len(chunks),
            "chunk_pages": chunk_pages,
            "weak_pages": weak_pages,
            "chunks_with_evidence_links": sum(1 for chunk in chunks if chunk.matched_criteria),
        },
    )


def first_page_classification(pages: list[PageUnit]) -> DocumentClassification | None:
    for page in pages:
        if page.classification:
            return page.classification
    return None


__all__ = [
    "EVIDENCE_PACKET_NEIGHBOR_PAGES",
    "EVIDENCE_PACKET_MAX_PAGES",
    "EVIDENCE_LEDGER_CHUNK_PAGES",
    "page_source_index",
    "evidence_packet_builder_agent",
    "expand_evidence_page_ids",
    "evidence_ledger_builder",
    "first_page_classification",
]
