"""Canonical Base RFP schema and validation models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, Literal, TypeAlias

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


SCHEMA_VERSION = "base_rfp.v1"
NORMALIZATION_VERSION = "base_rfp.normalize.v1"
JsonScalar: TypeAlias = str | int | float | bool | None


class RfpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _preserve_dynamic_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        known: set[str] = set()
        input_keys: dict[str, list[str]] = {}
        for name, field in cls.model_fields.items():
            keys = [name]
            alias = field.validation_alias
            if isinstance(alias, AliasChoices):
                keys.extend(str(choice) for choice in alias.choices if isinstance(choice, str))
            elif isinstance(alias, str):
                keys.append(alias)
            input_keys[name] = keys
            known.update(keys)
            if not field.is_required():
                for key in keys:
                    if key in result and result[key] is None:
                        result[key] = field.get_default(call_default_factory=True)

        if "value" in cls.model_fields and "values" in cls.model_fields:
            dynamic_value = result.get("value")
            if isinstance(dynamic_value, list):
                result["values"] = dynamic_value
                result["value"] = None
            elif isinstance(dynamic_value, dict):
                result["value"] = json.dumps(dynamic_value, ensure_ascii=True, sort_keys=True)

        if "facts" not in cls.model_fields:
            return result
        extras = [key for key in result if key not in known]
        if not extras:
            return result
        facts = list(result.get("facts") or [])
        parent_refs = list(result.get("source_refs") or [])
        for key in extras:
            extra = result.pop(key)
            refs = _nested_source_refs(extra) or parent_refs
            fact: dict[str, Any] = {"key": key, "label": key.replace("_", " "), "source_refs": refs}
            if isinstance(extra, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in extra):
                fact["values"] = extra
            elif extra is None or isinstance(extra, (str, int, float, bool)):
                fact["value"] = extra
            else:
                fact["value"] = json.dumps(extra, ensure_ascii=True, sort_keys=True, default=str)
                fact["notes"] = ["Preserved from a dynamic extraction field."]
            facts.append(fact)
        result["facts"] = facts
        return result


def _nested_source_refs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [ref for item in value for ref in _nested_source_refs(item)]
    if not isinstance(value, dict):
        return []
    refs = list(value.get("source_refs") or [])
    return refs + [ref for key, item in value.items() if key != "source_refs" for ref in _nested_source_refs(item)]


class BaseRfpSourceRef(RfpModel):
    document: str = Field(default="", validation_alias=AliasChoices("document", "document_id", "file_name"))
    page: int | None = Field(default=None, ge=1, validation_alias=AliasChoices("page", "page_number"))
    page_id: str = ""
    block: str = Field(default="", validation_alias=AliasChoices("block", "block_id"))
    table: str = Field(default="", validation_alias=AliasChoices("table", "table_id"))
    cell: str = Field(default="", validation_alias=AliasChoices("cell", "cell_id"))
    quote: str = ""


def _amendment_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("chronology must be an ISO-8601 date or datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


AmendmentOperation: TypeAlias = Literal["add", "replace", "delete", "restate", "clarify"]
AmendmentResolutionStatus: TypeAlias = Literal["unresolved", "reviewed", "resolved", "rejected"]
AmendmentEffectiveStatus: TypeAlias = Literal["pending", "effective", "superseded", "withdrawn"]


class _StrictRfpModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class BaseRfpAmendmentSourceRef(_StrictRfpModel):
    document: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    page_id: str = ""
    block: str = ""
    table: str = ""
    cell: str = ""
    quote: str = ""


class BaseRfpAmendmentEvent(_StrictRfpModel):
    event_id: str = Field(min_length=1)
    operation: AmendmentOperation
    target_path: str = Field(min_length=1)
    target_id: str = ""
    target_text: str = ""
    value: JsonValue = Field(
        default=None,
        validation_alias=AliasChoices("value", "replacement", "new_value"),
    )
    issued_at: str = Field(min_length=1, validation_alias=AliasChoices("issued_at", "chronology"))
    effective_at: str = ""
    resolution_status: AmendmentResolutionStatus = Field(
        default="unresolved",
        validation_alias=AliasChoices("resolution_status", "review_status"),
    )
    effective_status: AmendmentEffectiveStatus = "pending"
    confidence: float = Field(ge=0, le=1)
    source_refs: list[BaseRfpAmendmentSourceRef] = Field(min_length=1)
    supersedes_event_ids: list[str] = Field(default_factory=list)

    @field_validator("issued_at", "effective_at")
    @classmethod
    def _valid_chronology(cls, value: str) -> str:
        if value:
            _amendment_datetime(value)
        return value

    @field_validator("supersedes_event_ids")
    @classmethod
    def _nonblank_superseded_ids(cls, value: list[str]) -> list[str]:
        if any(not event_id for event_id in value):
            raise ValueError("supersedes_event_ids must not contain blank IDs")
        return value

    @model_validator(mode="after")
    def _operation_payload(self) -> BaseRfpAmendmentEvent:
        has_value = "value" in self.model_fields_set
        if self.operation == "delete" and self.value is not None:
            raise ValueError("delete events must not include a non-null value")
        if self.operation != "delete" and not has_value:
            raise ValueError(f"{self.operation} events require value")
        if self.event_id in self.supersedes_event_ids:
            raise ValueError("an event cannot supersede itself")
        return self


class BaseRfpAmendmentAuditEntry(_StrictRfpModel):
    event: BaseRfpAmendmentEvent
    status: Literal["applied", "skipped", "superseded"]
    reason: str = ""
    resolved_path: str = ""
    superseded_by_event_id: str = ""


class BaseRfpFact(RfpModel):
    fact_id: str = ""
    key: str = Field(default="", validation_alias=AliasChoices("key", "name", "field"))
    label: str = ""
    value: JsonScalar = None
    values: list[JsonScalar] = Field(default_factory=list)
    unit: str = ""
    status: str = "stated"
    condition: str = ""
    notes: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class BaseRfpSourceDocument(RfpModel):
    document_id: str = ""
    file_name: str = ""
    title: str = ""
    document_type: str = "unknown"
    revision: str = ""
    document_date: str = ""
    checksum_sha256: str = ""
    page_count: int | None = Field(default=None, ge=0)
    status: str = "available"
    supersedes_document_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BaseRfpIdentity(RfpModel):
    title: str = ""
    reference_number: str = Field(default="", validation_alias=AliasChoices("reference_number", "rfp_number", "tender_number"))
    solicitation_type: str = "rfp"
    revision: str = ""
    issue_date: str = ""
    status: str = ""
    language: str = ""
    country: str = ""
    jurisdiction: str = ""
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpParty(RfpModel):
    party_id: str = ""
    role: str = ""
    name: str = ""
    legal_name: str = ""
    address: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    identifiers: list[BaseRfpFact] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpParties(RfpModel):
    buyer: BaseRfpParty | None = None
    issuer: BaseRfpParty | None = None
    contacts: list[BaseRfpParty] = Field(default_factory=list)
    other: list[BaseRfpParty] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpMoney(RfpModel):
    amount: Decimal | None = None
    currency: str = ""
    basis: str = ""
    inclusive_of_tax: bool | None = None
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpLot(RfpModel):
    lot_id: str = ""
    number: str = ""
    title: str = ""
    description: str = ""
    estimated_value: BaseRfpMoney | None = None
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpProcurement(RfpModel):
    procurement_type: str = "unknown"
    method: str = ""
    category: str = ""
    bid_system: str = ""
    portal: str = ""
    currency: str = ""
    estimated_value: BaseRfpMoney | None = None
    lots: list[BaseRfpLot] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpDeliverable(RfpModel):
    deliverable_id: str = ""
    name: str = ""
    description: str = ""
    quantity: Decimal | None = None
    unit: str = ""
    acceptance: str = ""
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpScope(RfpModel):
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)
    deliverables: list[BaseRfpDeliverable] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    quantities: list[BaseRfpFact] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpScheduleEvent(RfpModel):
    event_id: str = ""
    event_type: str = ""
    name: str = ""
    start: str = ""
    end: str = ""
    duration: str = ""
    mandatory: bool | None = None
    dependency_event_ids: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpSchedule(RfpModel):
    timezone: str = ""
    calendar: str = ""
    events: list[BaseRfpScheduleEvent] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpBidRules(RfpModel):
    submission_method: str = ""
    portal: str = ""
    bid_structure: str = ""
    envelope_count: int | None = Field(default=None, ge=0)
    language: str = ""
    validity_days: int | None = Field(default=None, ge=0)
    currencies: list[str] = Field(default_factory=list)
    alternate_bids_allowed: bool | None = None
    consortium_allowed: bool | None = None
    subcontracting_allowed: bool | None = None
    signing_requirements: list[str] = Field(default_factory=list)
    clarification_process: str = ""
    submission_instructions: list[str] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpClause(RfpModel):
    clause_id: str = ""
    number: str = Field(default="", validation_alias=AliasChoices("number", "clause_number"))
    title: str = ""
    text: str = Field(default="", validation_alias=AliasChoices("text", "clause"))
    section_path: list[str] = Field(default_factory=list)
    clause_type: str = ""
    actor: str = ""
    obligation: str = ""
    applies_to: list[str] = Field(default_factory=list)
    mandatory: bool | None = None
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    cross_references: list[str] = Field(default_factory=list)
    defined_terms: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    status: str = "stated"
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpRequirement(RfpModel):
    requirement_id: str = ""
    title: str = ""
    text: str = Field(default="", validation_alias=AliasChoices("text", "requirement"))
    category: str = "other"
    subcategory: str = ""
    stage: str = ""
    requirement_type: str = ""
    actor: str = "bidder"
    action: str = ""
    subject: str = ""
    mandatory: bool | None = None
    conditional: bool | None = None
    condition: str = ""
    exceptions: list[str] = Field(default_factory=list)
    thresholds: list[BaseRfpFact] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    failure_effect: str = ""
    clarifiable: bool | None = None
    applies_to: list[str] = Field(default_factory=list)
    lot_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)
    required_document_ids: list[str] = Field(default_factory=list)
    dependency_requirement_ids: list[str] = Field(default_factory=list)
    status: str = "stated"
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpEvaluationCriterion(RfpModel):
    criterion_id: str = ""
    name: str = ""
    description: str = ""
    stage: str = ""
    weight: Decimal | None = None
    maximum_score: Decimal | None = None
    minimum_score: Decimal | None = None
    formula: str = ""
    mandatory: bool | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpEvaluation(RfpModel):
    method: str = ""
    stages: list[str] = Field(default_factory=list)
    criteria: list[BaseRfpEvaluationCriterion] = Field(default_factory=list)
    qualification_rule: str = ""
    opening_rules: list[str] = Field(default_factory=list)
    tie_breaker: str = ""
    normalization: str = ""
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpSecurityTerm(RfpModel):
    required: bool | None = None
    amount: BaseRfpMoney | None = None
    percentage: Decimal | None = None
    instrument: str = ""
    validity: str = ""
    exemption: str = ""
    conditions: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpFinancialTerms(RfpModel):
    currency: str = ""
    price_basis: str = ""
    taxes: str = ""
    duties: str = ""
    fees: list[BaseRfpMoney] = Field(default_factory=list)
    bid_security: BaseRfpSecurityTerm | None = None
    performance_security: BaseRfpSecurityTerm | None = None
    payment_terms: list[BaseRfpFact] = Field(default_factory=list)
    price_adjustment: str = ""
    retention: str = ""
    liquidated_damages: str = ""
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpContractTerms(RfpModel):
    contract_type: str = ""
    award_basis: str = ""
    term: str = ""
    extension: str = ""
    commencement: str = ""
    completion: str = ""
    warranty: str = ""
    support: str = ""
    service_levels: list[BaseRfpFact] = Field(default_factory=list)
    penalties: list[BaseRfpFact] = Field(default_factory=list)
    termination: str = ""
    indemnity: str = ""
    liability: str = ""
    insurance: str = ""
    dispute_resolution: str = ""
    governing_law: str = ""
    jurisdiction: str = ""
    change_control: str = ""
    assignment: str = ""
    subcontracting: str = ""
    confidentiality: str = ""
    intellectual_property: str = ""
    force_majeure: str = ""
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpPolicy(RfpModel):
    policy_id: str = ""
    name: str = ""
    reference: str = ""
    applicability: str = ""
    obligations: list[str] = Field(default_factory=list)
    exemptions: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpPolicies(RfpModel):
    items: list[BaseRfpPolicy] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpRequiredDocument(RfpModel):
    required_document_id: str = ""
    name: str = ""
    category: str = "other"
    stage: str = ""
    mandatory: bool | None = None
    condition: str = ""
    format: str = ""
    template_required: bool | None = None
    signed: bool | None = None
    notarized: bool | None = None
    issuer: str = ""
    validity: str = ""
    copies: int | None = Field(default=None, ge=0)
    checks: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    annexure_ids: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpAnnexure(RfpModel):
    annexure_id: str = ""
    number: str = Field(default="", validation_alias=AliasChoices("number", "annexure_number", "annexure_no"))
    title: str = ""
    annexure_type: str = ""
    availability: str = "unknown"
    required_submission: bool | None = None
    description: str = ""
    source_document_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    required_document_ids: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    facts: list[BaseRfpFact] = Field(default_factory=list)


class BaseRfpDefinition(RfpModel):
    definition_id: str = ""
    term: str = ""
    definition: str = ""
    aliases: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpGap(RfpModel):
    gap_id: str = ""
    gap_type: str = "missing_or_unclear"
    description: str = ""
    severity: str = ""
    expected: str = ""
    impact: str = ""
    affected_paths: list[str] = Field(default_factory=list)
    affected_ids: list[str] = Field(default_factory=list)
    resolution: str = ""
    status: str = "open"
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)


class BaseRfpCoverage(RfpModel):
    status: str = "unknown"
    documents_total: int = Field(default=0, ge=0)
    documents_covered: int = Field(default=0, ge=0)
    pages_total: int | None = Field(default=None, ge=0)
    pages_covered: int = Field(default=0, ge=0)
    blocks_covered: int = Field(default=0, ge=0)
    tables_covered: int = Field(default=0, ge=0)
    cells_covered: int = Field(default=0, ge=0)
    clauses_total: int = Field(default=0, ge=0)
    requirements_total: int = Field(default=0, ge=0)
    source_refs_total: int = Field(default=0, ge=0)
    uncovered_document_ids: list[str] = Field(default_factory=list)
    uncovered_page_ids: list[str] = Field(default_factory=list)
    uncovered_sections: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BaseRfpAudit(RfpModel):
    created_at: str = ""
    created_by: str = ""
    normalization_version: str = NORMALIZATION_VERSION
    chunk_count: int = Field(default=0, ge=0)
    content_sha256: str = ""
    input_record_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source_refs: list[BaseRfpSourceRef] = Field(default_factory=list)
    amendment_history: list[BaseRfpAmendmentAuditEntry] = Field(default_factory=list)


class BaseRfpUnderstanding(RfpModel):
    schema_version: Literal["base_rfp.v1"] = SCHEMA_VERSION
    record_id: str = ""
    source_documents: list[BaseRfpSourceDocument] = Field(default_factory=list)
    identity: BaseRfpIdentity = Field(default_factory=BaseRfpIdentity)
    parties: BaseRfpParties = Field(default_factory=BaseRfpParties)
    procurement: BaseRfpProcurement = Field(default_factory=BaseRfpProcurement)
    scope: BaseRfpScope = Field(default_factory=BaseRfpScope)
    schedule: BaseRfpSchedule = Field(default_factory=BaseRfpSchedule)
    bid_rules: BaseRfpBidRules = Field(default_factory=BaseRfpBidRules)
    clauses: list[BaseRfpClause] = Field(default_factory=list)
    requirements: list[BaseRfpRequirement] = Field(default_factory=list)
    evaluation: BaseRfpEvaluation = Field(default_factory=BaseRfpEvaluation)
    financial_terms: BaseRfpFinancialTerms = Field(default_factory=BaseRfpFinancialTerms)
    contract_terms: BaseRfpContractTerms = Field(default_factory=BaseRfpContractTerms)
    policies: BaseRfpPolicies = Field(default_factory=BaseRfpPolicies)
    required_documents: list[BaseRfpRequiredDocument] = Field(default_factory=list)
    annexures: list[BaseRfpAnnexure] = Field(default_factory=list)
    definitions: list[BaseRfpDefinition] = Field(default_factory=list)
    gaps: list[BaseRfpGap] = Field(default_factory=list)
    coverage: BaseRfpCoverage = Field(default_factory=BaseRfpCoverage)
    audit: BaseRfpAudit = Field(default_factory=BaseRfpAudit)

    @field_validator("parties", mode="before")
    @classmethod
    def _wrap_parties(cls, value: Any) -> Any:
        return {"other": value} if isinstance(value, list) else value

    @field_validator("schedule", mode="before")
    @classmethod
    def _wrap_schedule(cls, value: Any) -> Any:
        return {"events": value} if isinstance(value, list) else value

    @field_validator("policies", mode="before")
    @classmethod
    def _wrap_policies(cls, value: Any) -> Any:
        return {"items": value} if isinstance(value, list) else value


BASE_RFP_TOP_LEVEL_KEYS = tuple(BaseRfpUnderstanding.model_fields)


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
]

