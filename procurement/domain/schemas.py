"""Pydantic schemas shared by the proven procurement evaluation engine."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    document_id: str | None = None
    file_name: str | None = None
    page: int | None = Field(default=None, ge=1)
    quote: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DocumentClassification(BaseModel):
    content_role: Literal["tender_source", "vendor_submission", "reference", "unknown"] = "unknown"
    document_type: str
    title: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    key_facts: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class LayoutBlock(BaseModel):
    block_id: str
    block_type: str = "paragraph"
    text: str = ""
    reading_order: int = 0
    bbox: list[float] = Field(default_factory=list)
    font_size: float = 0.0
    font_name: str = ""
    is_bold: bool = False
    section_id: str = ""
    section_number: str = ""
    heading_level: int | None = None
    clause_number: str = ""
    duplicate_of: str = ""
    section_path: list[str] = Field(default_factory=list)
    structural_review: dict[str, Any] = Field(default_factory=dict)


class LayoutTableCell(BaseModel):
    cell_id: str
    row_index: int
    column_index: int
    text: str = ""
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False
    bbox: list[float] = Field(default_factory=list)


class LayoutTable(BaseModel):
    table_id: str
    title: str = ""
    markdown: str = ""
    reading_order: int = 0
    cells: list[LayoutTableCell] = Field(default_factory=list)
    bbox: list[float] = Field(default_factory=list)
    section_id: str = ""
    section_path: list[str] = Field(default_factory=list)


class StructuralSection(BaseModel):
    section_id: str
    logical_document_id: str
    document_id: str
    number: str = ""
    title: str
    level: int = 1
    parent_section_id: str = ""
    path: list[str] = Field(default_factory=list)
    page_start: int
    page_end: int
    source_block_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class StructuralPacket(BaseModel):
    schema_version: str = "tender_structure.v1"
    sections: list[StructuralSection] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    adjudication: dict[str, Any] = Field(default_factory=dict)


class PageUnit(BaseModel):
    page_id: str
    document_id: str
    file_name: str
    upload_side: Literal["tender", "vendor"]
    page_number: int
    text: str = ""
    layout_text: str = ""
    blocks: list[LayoutBlock] = Field(default_factory=list)
    tables: list[LayoutTable] = Field(default_factory=list)
    text_chars: int = 0
    extraction_method: str = "local"
    extraction_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_metrics: dict[str, Any] = Field(default_factory=dict)
    is_blank: bool = False
    ocr_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    classification: DocumentClassification | None = None


class LogicalDocument(BaseModel):
    logical_document_id: str
    upload_side: Literal["tender", "vendor"]
    title: str
    document_type: str = "unknown"
    content_role: Literal["tender_source", "vendor_submission", "reference", "unknown"] = "unknown"
    input_role: Literal["tender_packet", "evaluation_guideline", "vendor_submission"] = "tender_packet"
    source_document_ids: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    page_refs: list[str] = Field(default_factory=list)
    content_summary: str = ""
    key_facts: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal["found", "reconstructed", "unknown"] = "found"


class DocumentRecord(BaseModel):
    document_id: str
    file_name: str
    upload_side: Literal["tender", "vendor"]
    input_role: Literal["tender_packet", "evaluation_guideline", "vendor_submission"] = "tender_packet"
    extraction_status: Literal["ok", "empty", "unsupported", "error"]
    text_chars: int = 0
    page_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    classification: DocumentClassification | None = None
    logical_document_ids: list[str] = Field(default_factory=list)


class Criterion(BaseModel):
    criterion_id: str
    category: Literal["eligibility", "technical", "commercial", "financial", "compliance", "other"]
    requirement: str
    stage: str = "other"
    requirement_type: str = "other"
    failure_effect: str = "reject_unresponsive"
    mandatory: bool = True
    clarifiable: bool = False
    applies_when: str = ""
    evaluation_scope: str = "bid"
    threshold: dict[str, Any] = Field(default_factory=dict)
    pass_condition: str = ""
    required_documents: list[str] = Field(default_factory=list)
    source_document_id: str | None = None
    source_page_id: str | None = None
    source_page_number: int | None = None
    source_clause_no: str | None = None
    source_quote: str | None = None
    priority: str = "gate"
    criterion_kind: str = "gate"
    materiality: str = "mandatory_blocking"
    applicability: dict[str, Any] = Field(default_factory=lambda: {"state": "applicable"})
    decision_effect: str = "reject_unresponsive"
    semantic_key: str = ""
    evidence_groups: list[dict[str, Any]] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    source_references: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    extensions: dict[str, Any] = Field(default_factory=dict)


class TenderProfile(BaseModel):
    tender_title: str = "Unknown"
    tender_number: str = ""
    buyer: str = "Unknown"
    procurement_type: str = "Unknown"
    bid_system: str = "Unknown"
    scope: str = ""
    evaluation_method: str = ""
    governing_policy_id: str = ""
    mandatory_documents: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


class TenderSourceRef(BaseModel):
    document_id: str = ""
    page_number: int | None = None
    page_id: str = ""
    block_id: str = ""
    table_id: str = ""
    cell_id: str = ""
    quote: str = ""


class TenderEvidenceDocument(BaseModel):
    canonical_key: str = ""
    display_name: str = ""
    document_type: str = "unknown"
    category: str = "other"
    stage: str = "bid_stage"
    checks: list[str] = Field(default_factory=list)


class TenderRequirementExtraction(BaseModel):
    requirement_id: str
    requirement: str
    category: str = "other"
    stage: str = "other"
    mandatory: bool = True
    failure_effect: str = "reject_unresponsive"
    required_documents: list[TenderEvidenceDocument] = Field(default_factory=list)
    extraction_status: str = "extracted"
    missing_detail: str = ""
    source_refs: list[TenderSourceRef] = Field(default_factory=list)


class TenderRequiredDocument(TenderEvidenceDocument):
    required_for_requirement_ids: list[str] = Field(default_factory=list)
    source_annexures: list[str] = Field(default_factory=list)
    extraction_status: str = "extracted"
    mandatory_status: str = "required"


class TenderPacketGaps(BaseModel):
    missing_referenced_attachments: list[dict[str, Any]] = Field(default_factory=list)
    not_extractable_fields: list[dict[str, Any]] = Field(default_factory=list)


class TenderExtraction(BaseModel):
    schema_version: str = "tender_extraction_v1"
    dataset_case_id: str | None = None
    source_stage: str = "runtime"
    extraction_status: str = "complete"
    source_documents: list[dict[str, Any]] = Field(default_factory=list)
    tender: dict[str, Any] = Field(default_factory=dict)
    annexures: list[dict[str, Any]] = Field(default_factory=list)
    requirements: list[TenderRequirementExtraction] = Field(default_factory=list)
    required_documents: list[TenderRequiredDocument] = Field(default_factory=list)
    tender_packet_gaps: TenderPacketGaps = Field(default_factory=TenderPacketGaps)
    audit: dict[str, Any] = Field(default_factory=dict)


class EvidenceRequirement(BaseModel):
    evidence_id: str
    document_name: str
    document_type: str = "unknown"
    required: bool = True
    checks: list[str] = Field(default_factory=list)
    acceptance_rule: str = ""
    missing_effect: str = "reject"
    accepted_document_types: list[str] = Field(default_factory=list)
    entity_roles: list[str] = Field(default_factory=list)
    proof_contract: dict[str, Any] = Field(default_factory=dict)
    visual_checks: list[str] = Field(default_factory=list)
    issuer_roles: list[str] = Field(default_factory=list)
    validity_rule: str = ""
    source_refs: list[dict[str, Any]] = Field(default_factory=list)


class RequirementMatrixItem(BaseModel):
    criterion_id: str
    requirement: str
    category: str = "other"
    stage: str = "other"
    mandatory: bool = True
    clarifiable: bool = False
    failure_effect: str = "reject_unresponsive"
    pass_rule: str = "all_required_evidence_must_pass"
    source_quote: str | None = None
    criterion_kind: str = "gate"
    evaluation_scope: str = "technical_qualification"
    materiality: str = "mandatory_blocking"
    applicability: dict[str, Any] = Field(default_factory=lambda: {"state": "applicable"})
    decision_effect: str = "reject_unresponsive"
    semantic_key: str = ""
    threshold: dict[str, Any] = Field(default_factory=dict)
    evidence_groups: list[dict[str, Any]] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    source_references: list[dict[str, Any]] = Field(default_factory=list)
    required_evidence: list[EvidenceRequirement] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class RequirementMatrix(BaseModel):
    items: list[RequirementMatrixItem] = Field(default_factory=list)
    planning_notes: list[str] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class EvidenceMapEntry(BaseModel):
    criterion_id: str
    evidence_id: str
    document_name: str
    document_type: str = "unknown"
    required: bool = True
    checks: list[str] = Field(default_factory=list)
    matched_logical_document_ids: list[str] = Field(default_factory=list)
    matched_page_ids: list[str] = Field(default_factory=list)
    search_coverage: dict[str, Any] = Field(default_factory=dict)
    status: Literal["found", "missing", "unclear"] = "missing"
    reason: str = ""


class CriteriaEvidenceMap(BaseModel):
    entries: list[EvidenceMapEntry] = Field(default_factory=list)


class EvidencePacket(BaseModel):
    packet_id: str
    criterion_id: str
    evidence_id: str
    specialist: str
    document_name: str
    document_type: str = "unknown"
    logical_document_ids: list[str] = Field(default_factory=list)
    page_ids: list[str] = Field(default_factory=list)
    text: str = ""
    extracted_facts: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class EvidencePacketSet(BaseModel):
    packets: list[EvidencePacket] = Field(default_factory=list)


class EvidenceLedgerChunk(BaseModel):
    chunk_id: str
    document_id: str
    file_name: str
    upload_side: Literal["tender", "vendor"]
    page_start: int
    page_end: int
    page_ids: list[str] = Field(default_factory=list)
    text_chars: int = 0
    weak_page_ids: list[str] = Field(default_factory=list)
    extraction_methods: list[str] = Field(default_factory=list)
    document_type: str = "unknown"
    content_role: Literal["tender_source", "vendor_submission", "reference", "unknown"] = "unknown"
    facts: dict[str, str] = Field(default_factory=dict)
    matched_criteria: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EvidenceLedger(BaseModel):
    chunks: list[EvidenceLedgerChunk] = Field(default_factory=list)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)


class CriterionValidatorResult(BaseModel):
    criterion_id: str
    status: Literal["pass", "fail", "missing", "unclear", "not_evaluated", "unchanged"] = "unchanged"
    reason: str = ""
    checks: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    hard_failure: bool = False
    evidence_documents: list[str] = Field(default_factory=list)
    evidence_document_ids: list[str] = Field(default_factory=list)
    page_ids: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)


class CriterionValidatorSet(BaseModel):
    results: list[CriterionValidatorResult] = Field(default_factory=list)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)


class EvidenceScoutCandidate(BaseModel):
    page_id: str
    file_name: str = ""
    page_number: int | None = None
    snippet: str = ""
    why_candidate: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class EvidenceScoutRecord(BaseModel):
    criterion_id: str
    evidence_id: str
    document_name: str
    pages_checked: int = 0
    pages_without_text: list[str] = Field(default_factory=list)
    not_checked_pages: list[str] = Field(default_factory=list)
    candidate_pages: list[EvidenceScoutCandidate] = Field(default_factory=list)
    status: Literal["candidates_found", "missing", "unclear"] = "missing"
    qa_status: Literal["accepted", "blocked"] = "accepted"
    reason: str = ""
    search_status: str = ""
    search_coverage: dict[str, Any] = Field(default_factory=dict)
    degradations: list[str] = Field(default_factory=list)
    retrieval_plan: dict[str, Any] = Field(default_factory=dict)


class EvidenceScoutLog(BaseModel):
    records: list[EvidenceScoutRecord] = Field(default_factory=list)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)


class SpecialistFinding(BaseModel):
    packet_id: str
    criterion_id: str
    evidence_id: str
    specialist: str
    status: Literal["pass", "fail", "missing", "unclear"] = "unclear"
    check_results: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, str] = Field(default_factory=dict)
    reason: str = ""
    quote: str | None = None
    page_ids: list[str] = Field(default_factory=list)


class SpecialistFindingSet(BaseModel):
    findings: list[SpecialistFinding] = Field(default_factory=list)


class DocumentVerification(BaseModel):
    criterion_id: str
    evidence_id: str
    logical_document_ids: list[str] = Field(default_factory=list)
    page_ids: list[str] = Field(default_factory=list)
    status: Literal["pass", "fail", "missing", "unclear"] = "unclear"
    check_results: dict[str, str] = Field(default_factory=dict)
    reason: str = ""
    quote: str | None = None
    specialist_findings: list[dict[str, Any]] = Field(default_factory=list)
    visual_checks: list[dict[str, Any]] = Field(default_factory=list)


class DocumentVerificationSet(BaseModel):
    items: list[DocumentVerification] = Field(default_factory=list)


class VendorProfile(BaseModel):
    vendor_name: str = "Unknown"
    identifiers: dict[str, str] = Field(default_factory=dict)
    contact: dict[str, str] = Field(default_factory=dict)
    documents_submitted: list[str] = Field(default_factory=list)
    claimed_experience: list[str] = Field(default_factory=list)
    financials: dict[str, str] = Field(default_factory=dict)
    commercial_offer: dict[str, str] = Field(default_factory=dict)
    declarations: list[str] = Field(default_factory=list)
    extraction_warnings: list[str] = Field(default_factory=list)


class CriteriaFinding(BaseModel):
    criterion_id: str
    criterion_text: str
    category: str
    mandatory: bool
    status: Literal["pass", "fail", "missing", "unclear", "not_applicable", "not_evaluated"]
    reason: str
    required_documents: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    matched_documents: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    criterion_kind: str = "gate"
    evaluation_scope: str = "technical_qualification"
    materiality: str = "mandatory_blocking"
    decision_effect: str = "reject_unresponsive"
    applicability_status: Literal["applicable", "not_applicable", "unclear"] = "applicable"
    applicability_reason: str = ""
    applicability_page_ids: list[str] = Field(default_factory=list)


class ResponsivenessResult(BaseModel):
    status: Literal["responsive", "unresponsive", "clarification_required"] = "clarification_required"
    substantive_deviations: list[str] = Field(default_factory=list)
    minor_deviations: list[str] = Field(default_factory=list)
    strict_fail_reasons: list[str] = Field(default_factory=list)


class StageResult(BaseModel):
    status: Literal["pass", "fail", "not_applicable", "not_opened", "score_based", "unclear"] = "unclear"
    reason: str = ""
    failed_criteria: list[str] = Field(default_factory=list)
    score: float | None = None
    minimum_score: float | None = None
    opening_allowed: bool | None = None
    evaluated_price: str | None = None
    rank: str | None = None
    price_components: dict[str, str] = Field(default_factory=dict)


class AuditLogItem(BaseModel):
    rule_source: str
    finding: str
    document_id: str | None = None
    quote: str | None = None


class EvaluationReport(BaseModel):
    job_id: str = ""
    procurement_source: str = "unknown"
    procurement_category: str = "unknown"
    evaluation_method: str = "unknown"
    qualifies: bool
    recommendation: Literal["technically_qualified", "rejected", "needs_manual_review"]
    summary: str
    tender: TenderProfile
    vendor: VendorProfile
    documents: list[DocumentRecord] = Field(default_factory=list)
    logical_documents: list[LogicalDocument] = Field(default_factory=list)
    findings: list[CriteriaFinding] = Field(default_factory=list)
    responsiveness: ResponsivenessResult = Field(default_factory=ResponsivenessResult)
    eligibility_result: StageResult = Field(default_factory=StageResult)
    technical_result: StageResult = Field(default_factory=StageResult)
    commercial_result: StageResult = Field(default_factory=StageResult)
    financial_result: StageResult = Field(default_factory=StageResult)
    missing_mandatory: list[str] = Field(default_factory=list)
    manual_review_reasons: list[str] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    financial_summary: dict[str, str] = Field(default_factory=dict)
    final_decision: str = ""
    audit_log: list[AuditLogItem] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)


class PublicError(BaseModel):
    code: str
    message: str
    details: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: PublicError


class PublicTenderCriterion(BaseModel):
    no: int
    category: str
    requirement: str
    mandatory: bool
    pass_condition: str = ""
    required_documents: list[str] = Field(default_factory=list)


class PublicTenderSummary(BaseModel):
    title: str
    tender_number: str = ""
    buyer: str
    procurement_type: str
    bid_system: str
    scope: str
    summary: str
    mandatory_documents: list[str] = Field(default_factory=list)
    criteria: list[PublicTenderCriterion] = Field(default_factory=list)


class PublicVendorSummary(BaseModel):
    name: str
    identifiers: dict[str, str] = Field(default_factory=dict)
    submitted_documents: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)


class PublicMissingDocument(BaseModel):
    document_name: str
    required_for: str
    impact: str


class PublicCriterionResult(BaseModel):
    no: int
    category: str
    requirement: str
    status: Literal["passed", "failed", "missing", "needs_manual_review", "not_applicable", "not_evaluated"]
    result: str
    why: str
    required_documents: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    evidence_documents: list[str] = Field(default_factory=list)


class PublicManualReview(BaseModel):
    required: bool
    reasons: list[str] = Field(default_factory=list)


class PublicFinancialOpening(BaseModel):
    allowed: bool
    reason: str


class PublicValidationSummary(BaseModel):
    schema_valid: bool = True
    strict_mandatory_fail_applied: bool = True
    internal_audit_available: bool = True


class PublicEvaluationResult(BaseModel):
    schema_version: Literal["procurement-evaluation.v3"] = "procurement-evaluation.v3"
    bid_id: str
    final_decision: str
    recommendation: str
    qualifies: bool
    summary: str
    next_action: str
    tender: PublicTenderSummary
    vendor: PublicVendorSummary
    criteria_results: list[PublicCriterionResult]
    missing_documents: list[PublicMissingDocument] = Field(default_factory=list)
    manual_review: PublicManualReview
    financial_opening: PublicFinancialOpening
    validation: PublicValidationSummary = Field(default_factory=PublicValidationSummary)


class ExtractedDocument:
    def __init__(self, record: DocumentRecord, text: str, pages: list[PageUnit] | None = None, source_path: Path | None = None):
        self.record = record
        self.text = text
        self.pages = pages or []
        self.source_path = source_path
