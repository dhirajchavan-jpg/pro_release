"""Compatibility facade for modular evidence-pipeline helpers.

Packet/ledger construction, specialist checks, reconciliation, and source
routing live in focused sibling modules.  This facade keeps the historical
names, constants, call signatures, and patch points used by the application.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from procurement.domain.document_dictionary import procurement_dictionary_entries_for_text
from procurement.domain.schemas import (
    CriteriaEvidenceMap,
    DocumentClassification,
    DocumentVerification,
    EvidenceLedger,
    EvidenceLedgerChunk,
    EvidenceMapEntry,
    EvidencePacket,
    EvidencePacketSet,
    EvidenceRequirement,
    ExtractedDocument,
    LogicalDocument,
    PageUnit,
    RequirementMatrix,
    RequirementMatrixItem,
    SpecialistFinding,
    SpecialistFindingSet,
)
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match
from procurement.evaluation.criteria import criterion_atomic_requirement_text, criterion_retrieval_policy
from procurement.evaluation.facts import canonical_evidence_type, extract_packet_facts, normalize_fact_value
from procurement.evaluation.verification import best_quote, status_from_check_results
from procurement.understanding.classification import bidder_filled_evidence_marker, reference_source_material, tender_source_trap
from procurement.understanding.quality import MAX_DOC_CHARS

from . import evidence_pipeline_packets as _packets
from . import evidence_pipeline_routing as _routing
from . import evidence_pipeline_specialists as _specialists


EVIDENCE_PACKET_NEIGHBOR_PAGES = _packets.EVIDENCE_PACKET_NEIGHBOR_PAGES
EVIDENCE_PACKET_MAX_PAGES = _packets.EVIDENCE_PACKET_MAX_PAGES
EVIDENCE_LEDGER_CHUNK_PAGES = _packets.EVIDENCE_LEDGER_CHUNK_PAGES
BID_EVIDENCE_ROUTE_MAX_PAGES = _routing.BID_EVIDENCE_ROUTE_MAX_PAGES
EVIDENCE_VERIFIER_PAGE_CHARS = _routing.EVIDENCE_VERIFIER_PAGE_CHARS
STRICT_EVIDENCE_CHECKS = _specialists.STRICT_EVIDENCE_CHECKS


page_source_index = _packets.page_source_index
specific_evidence_mismatch = _routing.specific_evidence_mismatch
vendor_content_role_allowed = _routing.vendor_content_role_allowed
strong_vendor_evidence_marker = _routing.strong_vendor_evidence_marker
source_like_document_type = _routing.source_like_document_type
vendor_page_candidates = _routing.vendor_page_candidates
logical_docs_for_page_ids = _routing.logical_docs_for_page_ids
logical_docs_for_page_matches = _routing.logical_docs_for_page_matches
logical_document_text_index = _routing.logical_document_text_index
logical_document_routing_text = _routing.logical_document_routing_text
criterion_evidence_query = _routing.criterion_evidence_query
applicability_signal_score = _routing.applicability_signal_score
apply_specialist_fact_checks = _specialists.apply_specialist_fact_checks
best_specialist_finding = _specialists.best_specialist_finding
cross_document_conflict = _specialists.cross_document_conflict
specialist_type_for_evidence = _specialists.specialist_type_for_evidence
verification_needs_recovery = _specialists.verification_needs_recovery


def evidence_marker_score(evidence: EvidenceRequirement, text: str) -> int:
    return _routing.evidence_marker_score(evidence, text)


def document_type_compatible(expected: str, actual: str, text: str = "") -> bool:
    return _routing.document_type_compatible(
        expected,
        actual,
        text,
        compatibility_hooks={"evidence_marker_score": evidence_marker_score},
    )


def match_logical_documents(
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    vendor_logical_docs: list[LogicalDocument],
    doc_text_by_logical_id: dict[str, str],
) -> list[LogicalDocument]:
    return _routing.match_logical_documents(
        evidence,
        item,
        vendor_logical_docs,
        doc_text_by_logical_id,
        compatibility_hooks={
            "vendor_content_role_allowed": vendor_content_role_allowed,
            "logical_document_routing_text": logical_document_routing_text,
            "specific_evidence_mismatch": specific_evidence_mismatch,
            "source_like_document_type": source_like_document_type,
            "strong_vendor_evidence_marker": strong_vendor_evidence_marker,
            "document_type_compatible": document_type_compatible,
        },
    )


def match_evidence_pages(
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    candidates: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    return _routing.match_evidence_pages(
        evidence,
        item,
        candidates,
        max_pages=BID_EVIDENCE_ROUTE_MAX_PAGES,
        compatibility_hooks={
            "vendor_content_role_allowed": vendor_content_role_allowed,
            "specific_evidence_mismatch": specific_evidence_mismatch,
            "source_like_document_type": source_like_document_type,
            "strong_vendor_evidence_marker": strong_vendor_evidence_marker,
            "document_type_compatible": document_type_compatible,
            "evidence_marker_score": evidence_marker_score,
        },
    )


def logical_document_summary_score(
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
    logical_doc: LogicalDocument,
) -> int:
    return _routing.logical_document_summary_score(
        item,
        evidence,
        logical_doc,
        compatibility_hooks={"logical_document_routing_text": logical_document_routing_text},
    )


def criterion_page_excerpt(
    page: PageUnit,
    item: RequirementMatrixItem,
    evidence: EvidenceRequirement,
) -> str:
    return _routing.criterion_page_excerpt(
        page,
        item,
        evidence,
        verifier_page_chars=EVIDENCE_VERIFIER_PAGE_CHARS,
        compatibility_hooks={"criterion_evidence_query": criterion_evidence_query},
    )


def expand_evidence_page_ids(
    seed_page_ids: list[str],
    page_sources: dict[str, tuple[ExtractedDocument, PageUnit]],
    pages_by_document: dict[str, list[PageUnit]],
    *,
    max_pages: int = EVIDENCE_PACKET_MAX_PAGES,
) -> list[str]:
    return _packets.expand_evidence_page_ids(
        seed_page_ids,
        page_sources,
        pages_by_document,
        max_pages=max_pages,
        neighbor_pages=EVIDENCE_PACKET_NEIGHBOR_PAGES,
    )


def evidence_packet_builder_agent(
    matrix: RequirementMatrix,
    evidence_map: CriteriaEvidenceMap,
    logical_documents: list[LogicalDocument],
    docs: list[ExtractedDocument],
) -> EvidencePacketSet:
    return _packets.evidence_packet_builder_agent(
        matrix,
        evidence_map,
        logical_documents,
        docs,
        max_pages_default=EVIDENCE_PACKET_MAX_PAGES,
        compatibility_hooks={
            "page_source_index": page_source_index,
            "expand_evidence_page_ids": expand_evidence_page_ids,
            "specialist_type_for_evidence": specialist_type_for_evidence,
        },
    )


def first_page_classification(pages: list[PageUnit]) -> DocumentClassification | None:
    return _packets.first_page_classification(pages)


def evidence_ledger_builder(
    docs: list[ExtractedDocument],
    evidence_packets: EvidencePacketSet | None = None,
    chunk_pages: int = EVIDENCE_LEDGER_CHUNK_PAGES,
) -> EvidenceLedger:
    return _packets.evidence_ledger_builder(
        docs,
        evidence_packets,
        chunk_pages,
        compatibility_hooks={"first_page_classification": first_page_classification},
    )


def specialist_orchestrator_agent(
    matrix: RequirementMatrix,
    evidence_packets: EvidencePacketSet,
) -> SpecialistFindingSet:
    return _specialists.specialist_orchestrator_agent(
        matrix,
        evidence_packets,
        compatibility_hooks={"run_specialist_agent": run_specialist_agent},
    )


def cross_document_reconciliation_agent(
    matrix: RequirementMatrix,
    evidence_packets: EvidencePacketSet,
    specialist_findings: SpecialistFindingSet,
) -> SpecialistFindingSet:
    return _specialists.cross_document_reconciliation_agent(
        matrix,
        evidence_packets,
        specialist_findings,
        compatibility_hooks={"cross_document_conflict": cross_document_conflict},
    )


def run_specialist_agent(
    packet: EvidencePacket,
    item: RequirementMatrixItem | None,
    evidence: EvidenceRequirement | None,
) -> SpecialistFinding:
    return _specialists.run_specialist_agent(
        packet,
        item,
        evidence,
        compatibility_hooks={"specialist_rule_result": specialist_rule_result},
    )


def specialist_rule_result(
    specialist: str,
    evidence: EvidenceRequirement,
    item: RequirementMatrixItem,
    text: str,
) -> tuple[Literal["pass", "fail", "missing", "unclear"], str, str | None]:
    return _specialists.specialist_rule_result(
        specialist,
        evidence,
        item,
        text,
        compatibility_hooks={"evidence_marker_score": evidence_marker_score},
    )


def specialist_pass_can_upgrade_verification(verification: DocumentVerification, entry: EvidenceMapEntry) -> bool:
    return _specialists.specialist_pass_can_upgrade_verification(
        verification,
        entry,
        strict_evidence_checks=STRICT_EVIDENCE_CHECKS,
    )


def apply_specialist_findings_to_verification(
    verification: DocumentVerification,
    entry: EvidenceMapEntry,
    findings: list[SpecialistFinding],
) -> None:
    return _specialists.apply_specialist_findings_to_verification(
        verification,
        entry,
        findings,
        compatibility_hooks={
            "best_specialist_finding": best_specialist_finding,
            "apply_specialist_fact_checks": apply_specialist_fact_checks,
            "specialist_pass_can_upgrade_verification": specialist_pass_can_upgrade_verification,
        },
    )


__all__ = [
    "os",
    "re",
    "Any",
    "Literal",
    "procurement_dictionary_entries_for_text",
    "CriteriaEvidenceMap",
    "DocumentClassification",
    "DocumentVerification",
    "EvidenceLedger",
    "EvidenceLedgerChunk",
    "EvidenceMapEntry",
    "EvidencePacket",
    "EvidencePacketSet",
    "EvidenceRequirement",
    "ExtractedDocument",
    "LogicalDocument",
    "PageUnit",
    "RequirementMatrix",
    "RequirementMatrixItem",
    "SpecialistFinding",
    "SpecialistFindingSet",
    "model_dump",
    "clean_text",
    "keyword_tokens",
    "normalize_for_match",
    "criterion_atomic_requirement_text",
    "criterion_retrieval_policy",
    "canonical_evidence_type",
    "extract_packet_facts",
    "normalize_fact_value",
    "best_quote",
    "status_from_check_results",
    "bidder_filled_evidence_marker",
    "reference_source_material",
    "tender_source_trap",
    "MAX_DOC_CHARS",
    "EVIDENCE_PACKET_NEIGHBOR_PAGES",
    "EVIDENCE_PACKET_MAX_PAGES",
    "EVIDENCE_LEDGER_CHUNK_PAGES",
    "BID_EVIDENCE_ROUTE_MAX_PAGES",
    "EVIDENCE_VERIFIER_PAGE_CHARS",
    "STRICT_EVIDENCE_CHECKS",
    "page_source_index",
    "specific_evidence_mismatch",
    "evidence_packet_builder_agent",
    "expand_evidence_page_ids",
    "evidence_ledger_builder",
    "first_page_classification",
    "specialist_orchestrator_agent",
    "cross_document_reconciliation_agent",
    "cross_document_conflict",
    "run_specialist_agent",
    "specialist_rule_result",
    "specialist_type_for_evidence",
    "evidence_marker_score",
    "match_logical_documents",
    "vendor_page_candidates",
    "logical_docs_for_page_ids",
    "match_evidence_pages",
    "vendor_content_role_allowed",
    "strong_vendor_evidence_marker",
    "logical_docs_for_page_matches",
    "apply_specialist_findings_to_verification",
    "apply_specialist_fact_checks",
    "specialist_pass_can_upgrade_verification",
    "best_specialist_finding",
    "source_like_document_type",
    "document_type_compatible",
    "logical_document_text_index",
    "logical_document_routing_text",
    "logical_document_summary_score",
    "criterion_evidence_query",
    "applicability_signal_score",
    "criterion_page_excerpt",
    "verification_needs_recovery",
]
