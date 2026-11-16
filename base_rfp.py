"""Stable import facade for the canonical Base RFP domain."""

from __future__ import annotations

from procurement.understanding.base_rfp_models import (
    SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    JsonScalar,
    RfpModel,
    BaseRfpSourceRef,
    BaseRfpAmendmentSourceRef,
    BaseRfpAmendmentEvent,
    BaseRfpAmendmentAuditEntry,
    BaseRfpFact,
    BaseRfpSourceDocument,
    BaseRfpIdentity,
    BaseRfpParty,
    BaseRfpParties,
    BaseRfpMoney,
    BaseRfpLot,
    BaseRfpProcurement,
    BaseRfpDeliverable,
    BaseRfpScope,
    BaseRfpScheduleEvent,
    BaseRfpSchedule,
    BaseRfpBidRules,
    BaseRfpClause,
    BaseRfpRequirement,
    BaseRfpEvaluationCriterion,
    BaseRfpEvaluation,
    BaseRfpSecurityTerm,
    BaseRfpFinancialTerms,
    BaseRfpContractTerms,
    BaseRfpPolicy,
    BaseRfpPolicies,
    BaseRfpRequiredDocument,
    BaseRfpAnnexure,
    BaseRfpDefinition,
    BaseRfpGap,
    BaseRfpCoverage,
    BaseRfpAudit,
    BaseRfpUnderstanding,
    BASE_RFP_TOP_LEVEL_KEYS,
)
from procurement.understanding.base_rfp_normalization import (
    assign_stable_ids,
    create_base_rfp_understanding,
    merge_base_rfp_partials,
    normalize_base_rfp_partial,
)
from procurement.understanding.base_rfp_amendments import (
    _amendment_path,
    apply_base_rfp_amendments,
)


normalize_chunk_partial = normalize_base_rfp_partial
merge_chunk_partials = merge_base_rfp_partials
build_base_rfp_understanding = create_base_rfp_understanding
apply_amendment_events = apply_base_rfp_amendments


__all__ = [
    "SCHEMA_VERSION",
    "NORMALIZATION_VERSION",
    "JsonScalar",
    "RfpModel",
    "BaseRfpSourceRef",
    "BaseRfpAmendmentSourceRef",
    "BaseRfpAmendmentEvent",
    "BaseRfpAmendmentAuditEntry",
    "BaseRfpFact",
    "BaseRfpSourceDocument",
    "BaseRfpIdentity",
    "BaseRfpParty",
    "BaseRfpParties",
    "BaseRfpMoney",
    "BaseRfpLot",
    "BaseRfpProcurement",
    "BaseRfpDeliverable",
    "BaseRfpScope",
    "BaseRfpScheduleEvent",
    "BaseRfpSchedule",
    "BaseRfpBidRules",
    "BaseRfpClause",
    "BaseRfpRequirement",
    "BaseRfpEvaluationCriterion",
    "BaseRfpEvaluation",
    "BaseRfpSecurityTerm",
    "BaseRfpFinancialTerms",
    "BaseRfpContractTerms",
    "BaseRfpPolicy",
    "BaseRfpPolicies",
    "BaseRfpRequiredDocument",
    "BaseRfpAnnexure",
    "BaseRfpDefinition",
    "BaseRfpGap",
    "BaseRfpCoverage",
    "BaseRfpAudit",
    "BaseRfpUnderstanding",
    "BASE_RFP_TOP_LEVEL_KEYS",
    "assign_stable_ids",
    "normalize_base_rfp_partial",
    "merge_base_rfp_partials",
    "create_base_rfp_understanding",
    "apply_base_rfp_amendments",
    "normalize_chunk_partial",
    "merge_chunk_partials",
    "build_base_rfp_understanding",
    "apply_amendment_events",
]


def _self_check() -> None:
    ref = {"document_id": "source-1", "page_number": 2, "page_id": "p2", "block_id": "b4"}
    clause = {"clause_id": "temp", "number": "4.1", "text": "Bid security is required.", "source_refs": [ref]}
    requirement = {"text": "Submit bid security.", "source_refs": [ref]}
    parts = [
        {"source_documents": [{"document_id": "source-1", "file_name": "rfp.pdf", "page_count": 2}], "clauses": [clause], "requirements": [requirement]},
        {"clauses": [clause, {**clause, "text": "Bid security must remain valid."}], "requirements": [requirement]},
    ]
    result = create_base_rfp_understanding(parts)
    assert tuple(BaseRfpUnderstanding.model_fields) == (
        "schema_version", "record_id", "source_documents", "identity", "parties", "procurement", "scope",
        "schedule", "bid_rules", "clauses", "requirements", "evaluation", "financial_terms", "contract_terms",
        "policies", "required_documents", "annexures", "definitions", "gaps", "coverage", "audit",
    )
    assert len(result.clauses) == 2 and len(result.requirements) == 1
    assert result.model_dump(mode="json") == create_base_rfp_understanding(parts).model_dump(mode="json")


if __name__ == "__main__":
    _self_check()
