"""Criterion and requirement-matrix projection helpers."""

from __future__ import annotations

import re
from typing import Any, Literal

from procurement.domain.document_dictionary import (
    procurement_document_dictionary_context,
    procurement_dictionary_display_names,
    procurement_dictionary_entries_for_text,
)
from procurement.domain.schemas import (
    Criterion,
    EvidenceRequirement,
    ExtractedDocument,
    PageUnit,
    RequirementMatrix,
    RequirementMatrixItem,
    TenderExtraction,
    TenderProfile,
    TenderRequirementExtraction,
    TenderSourceRef,
)
from procurement.domain.serialization import validate_model
from procurement.domain.text import clean_text, keyword_tokens, normalize_for_match
from procurement.evaluation.normalization import normalize_tender_profile_data, stringify_profile_value
from procurement.evaluation.planning import (
    backfill_requirement_matrix,
    evidence_requirements_from_criterion,
    infer_checks,
)
from .extraction_requirements import document_key_needs_requirement_owner

def tender_profile_from_extraction(extraction: TenderExtraction) -> TenderProfile:
    tender = extraction.tender
    source_notes = []
    for gap in extraction.tender_packet_gaps.missing_referenced_attachments:
        attachment = stringify_profile_value(gap.get("attachment")).strip()
        impact = stringify_profile_value(gap.get("impact_on_extraction") or gap.get("impact_on_master_json")).strip()
        if attachment:
            source_notes.append(f"Missing referenced tender attachment: {attachment}. {impact}".strip())
    return TenderProfile(
        tender_title=stringify_profile_value(tender.get("tender_title")).strip() or "Unknown",
        tender_number=stringify_profile_value(tender.get("tender_number")).strip(),
        buyer=stringify_profile_value(tender.get("buyer")).strip() or "Unknown",
        procurement_type=stringify_profile_value(tender.get("procurement_type")).strip() or "Unknown",
        bid_system=stringify_profile_value(tender.get("bid_system")).strip() or "Unknown",
        scope=stringify_profile_value(tender.get("scope_summary") or tender.get("scope")).strip(),
        evaluation_method=stringify_profile_value(tender.get("evaluation_method")).strip(),
        mandatory_documents=sorted({doc.display_name for doc in extraction.required_documents if doc.display_name and doc.mandatory_status == "required"}),
        criteria=[criterion_from_extracted_requirement(item) for item in extraction.requirements],
        source_notes=source_notes,
    )


def criterion_from_extracted_requirement(requirement: TenderRequirementExtraction) -> Criterion:
    first_ref = requirement.source_refs[0] if requirement.source_refs else TenderSourceRef()
    required_documents = [doc.display_name or doc.canonical_key for doc in requirement.required_documents if doc.display_name or doc.canonical_key]
    return Criterion(
        criterion_id=requirement.requirement_id,
        category=normalize_criterion_category(requirement.category, requirement.requirement),
        requirement=requirement.requirement,
        stage=normalize_criterion_stage(requirement.stage),
        requirement_type=infer_requirement_type(requirement.requirement),
        failure_effect=requirement.failure_effect,
        mandatory=requirement.mandatory,
        clarifiable=requirement.extraction_status in {"referenced_not_available", "unresolved"},
        pass_condition=requirement.requirement,
        required_documents=required_documents,
        source_document_id=first_ref.document_id or None,
        source_page_number=first_ref.page_number,
        source_quote=first_ref.quote or None,
        priority="gate" if requirement.mandatory else "note",
        confidence=0.9 if requirement.extraction_status == "extracted" else 0.65,
    )


def normalize_criterion_category(category: str, text: str) -> Literal["eligibility", "technical", "commercial", "financial", "compliance", "other"]:
    if category in {"eligibility", "technical", "commercial", "financial", "compliance", "other"}:
        return category  # type: ignore[return-value]
    if category in {"submission", "bqc"}:
        return "eligibility"
    if category == "preference_policy":
        return "compliance"
    return infer_criterion_category(text)


def normalize_criterion_stage(stage: str) -> str:
    return {"bid_stage": "responsiveness"}.get(stage, stage or "other")


def requirement_matrix_from_extraction(extraction: TenderExtraction, tender: TenderProfile) -> RequirementMatrix:
    extracted_by_id = {item.requirement_id: item for item in extraction.requirements}
    items = []
    for criterion in tender.criteria:
        extracted = extracted_by_id.get(criterion.criterion_id)
        evidence_documents = extracted.required_documents if extracted else []
        required_evidence = []
        for index, document in enumerate(evidence_documents, start=1):
            required_evidence.append(
                EvidenceRequirement(
                    evidence_id=f"{criterion.criterion_id}-E{index}",
                    document_name=document.display_name or document.canonical_key,
                    document_type=document.document_type,
                    required=criterion.mandatory,
                    checks=document.checks or infer_checks(criterion, document.display_name),
                    acceptance_rule=criterion.pass_condition or criterion.requirement,
                    missing_effect="manual_review" if extracted and extracted.extraction_status != "extracted" else ("reject" if criterion.mandatory else "manual_review"),
                )
            )
        items.append(
            RequirementMatrixItem(
                criterion_id=criterion.criterion_id,
                requirement=criterion.requirement,
                category=criterion.category,
                stage=criterion.stage,
                mandatory=criterion.mandatory,
                clarifiable=criterion.clarifiable,
                failure_effect=criterion.failure_effect,
                source_quote=criterion.source_quote,
                required_evidence=required_evidence or evidence_requirements_from_criterion(criterion),
            )
        )
    return backfill_requirement_matrix(tender, RequirementMatrix(items=items, planning_notes=["derived from 03_tender_extraction"]))


def tender_extraction_warnings(extraction: TenderExtraction) -> list[str]:
    warnings = []
    for gap in extraction.tender_packet_gaps.missing_referenced_attachments:
        attachment = stringify_profile_value(gap.get("attachment")).strip()
        impact = stringify_profile_value(gap.get("impact_on_extraction") or gap.get("impact_on_master_json")).strip()
        if attachment:
            warnings.append(f"Tender packet may be incomplete: {attachment} is referenced but was not available. {impact}".strip())
    return warnings


def infer_criterion_category(text: str) -> Literal["eligibility", "technical", "commercial", "financial", "compliance", "other"]:
    lower = text.lower()
    if any(term in lower for term in ["turnover", "net worth", "financial statement", "audited", "profit and loss"]):
        return "financial"
    if any(term in lower for term in ["emd", "epbg", "bank guarantee", "bid validity", "payment", "price"]):
        return "commercial"
    if any(term in lower for term in ["technical", "specification", "boq", "datasheet", "quality", "iso"]):
        return "technical"
    if any(term in lower for term in ["blacklist", "holiday", "debar", "integrity", "nil deviation", "local content", "make in india"]):
        return "compliance"
    if any(term in lower for term in ["eligible", "similar work", "experience", "udyam", "msme", "mse"]):
        return "eligibility"
    return "other"


def infer_criterion_stage(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ["similar work", "turnover", "net worth", "experience"]):
        return "pqc"
    if any(term in lower for term in ["financial opening", "price", "l1"]):
        return "financial"
    if any(term in lower for term in ["technical", "specification", "boq", "nil deviation"]):
        return "technical"
    return "responsiveness"


def infer_requirement_type(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ["turnover", "net worth", "similar work", "years", "value not less"]):
        return "pqc_threshold"
    if any(term in lower for term in ["declaration", "undertaking", "integrity", "blacklist", "holiday", "nil deviation"]):
        return "declaration"
    if any(term in lower for term in ["local content", "make in india", "class-i", "class ii"]):
        return "preference_policy"
    if any(term in lower for term in ["technical", "specification", "boq"]):
        return "technical_spec"
    return "mandatory_document"


def tender_clause_candidates(tender_docs: list[ExtractedDocument]) -> list[dict[str, Any]]:
    markers = [
        "shall",
        "must",
        "mandatory",
        "required",
        "eligible",
        "emd",
        "epbg",
        "pbg",
        "turnover",
        "net worth",
        "similar work",
        "purchase order",
        "completion certificate",
        "invoice",
        "blacklisted",
        "debarred",
        "holiday",
        "local content",
        "make in india",
        "integrity pact",
        "nil deviation",
        "bid validity",
        "annexure",
        "financial opening",
    ]
    for entry in procurement_document_dictionary_context():
        markers.extend(str(alias).lower() for alias in entry.get("aliases", [])[:4] if str(alias).strip())
        display_name = str(entry.get("display_name", "")).lower()
        if display_name:
            markers.append(display_name)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in tender_docs:
        for page in document.pages or [PageUnit(page_id=f"{document.record.document_id}-p1", document_id=document.record.document_id, file_name=document.record.file_name, upload_side="tender", page_number=1, text=document.text)]:
            chunks = re.split(r"(?<=[.;:])\s+|\n+", page.text or "")
            for chunk in chunks:
                text = clean_text(chunk)
                if len(text) < 35:
                    continue
                lower = text.lower()
                matched = [marker for marker in markers if marker in lower]
                if not matched:
                    continue
                key = normalize_for_match(text)[:220]
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "source_document_id": document.record.document_id,
                        "source_page_id": page.page_id,
                        "source_page_number": page.page_number,
                        "markers": matched[:5],
                        "text": text[:700],
                    }
                )
                if len(candidates) >= 80:
                    return candidates
    return candidates


def criterion_from_coverage_data(data: dict[str, Any], tender: TenderProfile) -> Criterion | None:
    data = dict(data)
    requirement = stringify_profile_value(data.get("requirement")).strip()
    quote = stringify_profile_value(data.get("source_quote")).strip()
    if not requirement or not quote:
        return None
    data["criterion_id"] = next_criterion_id(tender)
    data.setdefault("category", infer_criterion_category(requirement))
    data.setdefault("stage", infer_criterion_stage(requirement))
    data.setdefault("requirement_type", infer_requirement_type(requirement))
    data.setdefault("failure_effect", "reject_unresponsive" if data.get("mandatory", True) else "manual_review")
    data.setdefault("mandatory", True)
    data.setdefault("clarifiable", False)
    data.setdefault("pass_condition", requirement)
    data.setdefault("required_documents", infer_required_documents_from_text(requirement))
    data.setdefault("priority", "gate")
    data.setdefault("confidence", 0.75)
    normalize_tender_profile_data({"criteria": [data]})
    return validate_model(Criterion, data)


def backfill_mandatory_document_criteria(tender: TenderProfile) -> int:
    added = 0
    for document_name in tender.mandatory_documents:
        name = clean_text(document_name)
        if not name or criterion_is_covered(name, tender.criteria):
            continue
        if mandatory_document_needs_source_requirement(name):
            continue
        requirement = f"Bidder must submit {name}."
        tender.criteria.append(
            Criterion(
                criterion_id=next_criterion_id(tender),
                category=infer_criterion_category(requirement),
                requirement=requirement,
                stage=infer_criterion_stage(requirement),
                requirement_type="mandatory_document",
                failure_effect="reject_unresponsive",
                mandatory=True,
                clarifiable=False,
                pass_condition=f"{name} must be present and valid.",
                required_documents=[name],
                source_quote=name,
                priority="gate",
                confidence=0.7,
            )
        )
        added += 1
    return added


def mandatory_document_needs_source_requirement(document_name: str) -> bool:
    matches = procurement_dictionary_entries_for_text(document_name)
    return any(document_key_needs_requirement_owner(str(entry.get("canonical_key") or "")) for entry in matches)


def criterion_is_covered(text: str, criteria: list[Criterion]) -> bool:
    target_tokens = keyword_tokens(text)
    if not target_tokens:
        return True
    for criterion in criteria:
        existing = " ".join([criterion.requirement, criterion.pass_condition, " ".join(criterion.required_documents), criterion.source_quote or ""])
        existing_tokens = keyword_tokens(existing)
        if len(target_tokens & existing_tokens) >= max(2, min(5, len(target_tokens) // 2)):
            return True
    return False


def next_criterion_id(tender: TenderProfile) -> str:
    used = [
        int(match.group(1))
        for criterion in tender.criteria
        if (match := re.fullmatch(r"C(\d+)", criterion.criterion_id or ""))
    ]
    return f"C{(max(used) if used else 0) + 1}"


def infer_required_documents_from_text(text: str) -> list[str]:
    dictionary_documents = procurement_dictionary_display_names(text)
    if dictionary_documents:
        return dictionary_documents
    lower = text.lower()
    documents = []
    checks = [
        ("Udyam certificate", ["udyam", "msme", "mse"]),
        ("EMD payment proof", ["emd", "earnest money", "bid security"]),
        ("ePBG / bank guarantee", ["epbg", "performance bank guarantee"]),
        ("Purchase order / work order / LOI", ["purchase order", "work order", "loi", "letter of intent"]),
        ("Completion certificate / final bill / invoice", ["completion", "final bill", "invoice", "execution"]),
        ("CA certificate with UDIN / audited financials", ["turnover", "net worth", "udin", "audited", "financial"]),
        ("Local content certificate", ["local content", "make in india", "class-i", "class ii"]),
        ("Integrity / blacklisting declaration", ["integrity", "blacklist", "debar", "holiday"]),
        ("Nil deviation declaration", ["nil deviation", "deviation"]),
    ]
    for name, needles in checks:
        if any(needle in lower for needle in needles):
            documents.append(name)
    return documents or [text[:120]]

