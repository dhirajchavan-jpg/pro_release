"""Strict, schema-neutral RFP atoms and deterministic Base RFP projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from base_rfp import create_base_rfp_understanding, normalize_base_rfp_partial


AtomType: TypeAlias = Literal[
    "identity",
    "party",
    "procurement",
    "scope",
    "schedule",
    "bid_rule",
    "clause",
    "requirement",
    "evaluation",
    "financial_term",
    "contract_term",
    "policy",
    "required_document",
    "annexure",
    "definition",
    "other",
    "non_semantic",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RfpAtom(_StrictModel):
    atom_id: str = Field(min_length=1)
    atom_type: AtomType
    key: str = ""
    title: str = ""
    normalized_text: str = ""
    value: JsonValue = None
    values: list[JsonValue] = Field(default_factory=list)
    actor: str = ""
    action: str = ""
    subject: str = ""
    modality: str = ""
    mandatory: bool | None = None
    condition: str = ""
    exceptions: list[str] = Field(default_factory=list)
    category: str = ""
    stage: str = ""
    failure_effect: str = ""
    evidence_names: list[str] = Field(default_factory=list)
    properties: dict[str, JsonValue] = Field(default_factory=dict)
    source_unit_ids: list[str] = Field(min_length=1)

    @field_validator("atom_id")
    @classmethod
    def _nonblank_atom_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("atom_id must not be blank")
        return value

    @field_validator("source_unit_ids")
    @classmethod
    def _nonblank_source_unit_ids(cls, value: list[str]) -> list[str]:
        if any(not unit_id.strip() for unit_id in value):
            raise ValueError("source_unit_ids must not contain blank IDs")
        return value


class RfpAtomBatch(_StrictModel):
    atoms: list[RfpAtom] = Field(default_factory=list)


class _SourceUnit(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)

    text: str = Field(min_length=1)
    document: str = Field(min_length=1)
    page: int = Field(ge=1)
    page_id: str = Field(min_length=1)
    block: str = ""
    table: str = ""
    cell: str = ""
    section_id: str = ""
    section_path: list[str] = Field(default_factory=list)
    clause_number: str = ""


def normalize_atom_batch(
    data: Mapping[str, Any] | RfpAtomBatch,
    allowed_unit_ids: Iterable[str],
) -> RfpAtomBatch:
    """Strictly validate a batch, its provenance IDs, and exact duplicates."""
    if isinstance(allowed_unit_ids, (str, bytes)):
        raise TypeError("allowed_unit_ids must be an iterable of strings")
    allowed = set(allowed_unit_ids)
    if any(not isinstance(unit_id, str) for unit_id in allowed):
        raise TypeError("allowed_unit_ids must contain only strings")

    batch = data if isinstance(data, RfpAtomBatch) else RfpAtomBatch.model_validate(data)
    unknown = sorted(
        {
            unit_id
            for atom in batch.atoms
            for unit_id in atom.source_unit_ids
            if unit_id not in allowed
        }
    )
    if unknown:
        raise ValueError(f"Unknown source_unit_ids: {', '.join(unknown)}")

    unique: list[RfpAtom] = []
    seen: set[str] = set()
    atom_ids: dict[str, str] = {}
    for atom in batch.atoms:
        marker = json.dumps(atom.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        previous = atom_ids.get(atom.atom_id)
        if previous is not None and previous != marker:
            raise ValueError(f"atom_id {atom.atom_id!r} is used by different atoms")
        atom_ids[atom.atom_id] = marker
        if marker not in seen:
            seen.add(marker)
            unique.append(atom)
    return RfpAtomBatch(atoms=unique)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _raw_value(atom: RfpAtom) -> JsonValue:
    if atom.value is not None:
        return atom.value
    if atom.values:
        return atom.values
    return atom.normalized_text or atom.title or atom.key


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _atom_text(atom: RfpAtom) -> str:
    if atom.normalized_text:
        return atom.normalized_text
    raw = _raw_value(atom)
    if raw not in (None, "", []):
        return _text(raw)
    return _canonical(atom.properties) if atom.properties else atom.title or atom.key


def _strings(value: JsonValue) -> list[str]:
    if value in (None, "", []):
        return []
    values = value if isinstance(value, list) else [value]
    return [_text(item) for item in values]


def _ids(value: JsonValue) -> list[str]:
    return [item for item in _strings(value) if item]


def _bool(value: JsonValue) -> bool | None:
    return value if isinstance(value, bool) else None


def _nonnegative_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value: JsonValue) -> JsonValue | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return value if number.is_finite() else None


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _fact_value(row: dict[str, Any], value: JsonValue) -> None:
    if isinstance(value, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
        row["values"] = value
    elif value is None or isinstance(value, (str, int, float, bool)):
        row["value"] = value
    else:
        row["value"] = _canonical(value)


def _fact(
    atom: RfpAtom,
    refs: list[dict[str, Any]],
    suffix: str,
    key: str,
    value: JsonValue,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_id": f"{atom.atom_id}:fact:{suffix}",
        "key": key,
        "label": atom.title,
        "condition": atom.condition,
        "source_refs": refs,
    }
    _fact_value(row, value)
    return row


def _atom_facts(atom: RfpAtom, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facts = [_fact(atom, refs, "main", atom.key or atom.title or atom.atom_type, _raw_value(atom))]
    public_properties = {
        key: value
        for key, value in atom.properties.items()
        if key not in {"is_requirement", "is_required_document"}
    }
    for index, (key, value) in enumerate(sorted(public_properties.items())):
        facts.append(_fact(atom, refs, f"property:{index}", key, value))
    for key, value in (
        ("modality", atom.modality),
        ("subject", atom.subject),
        ("category", atom.category),
        ("stage", atom.stage),
        ("failure_effect", atom.failure_effect),
        ("evidence_names", atom.evidence_names),
    ):
        if value not in ("", []):
            facts.append(_fact(atom, refs, key, key, value))
    return facts


def _load_source_units(source_units: Mapping[str, Mapping[str, Any]]) -> dict[str, _SourceUnit]:
    units: dict[str, _SourceUnit] = {}
    for unit_id, value in source_units.items():
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ValueError("source_units keys must be nonblank strings")
        units[unit_id] = _SourceUnit.model_validate(value)
    return units


def _source_refs(atom: RfpAtom, units: Mapping[str, _SourceUnit]) -> list[dict[str, Any]]:
    refs = []
    for unit_id in atom.source_unit_ids:
        unit = units[unit_id]
        ref: dict[str, Any] = {
            "document": unit.document,
            "page": unit.page,
            "page_id": unit.page_id,
            "quote": unit.text,
        }
        for key in ("block", "table", "cell"):
            if value := getattr(unit, key):
                ref[key] = value
        refs.append(ref)
    return refs


def _section_path(atom: RfpAtom, units: Mapping[str, _SourceUnit]) -> list[str]:
    return next((units[unit_id].section_path for unit_id in atom.source_unit_ids if units[unit_id].section_path), [])


def _clause_number(atom: RfpAtom, units: Mapping[str, _SourceUnit]) -> str:
    return next((units[unit_id].clause_number for unit_id in atom.source_unit_ids if units[unit_id].clause_number), "")


def _set_first(section: dict[str, Any], field: str, value: Any) -> None:
    if value not in (None, "", []) and field not in section:
        section[field] = value


_SIMPLE_SECTIONS = {
    "identity": "identity",
    "procurement": "procurement",
    "scope": "scope",
    "bid_rule": "bid_rules",
    "financial_term": "financial_terms",
    "contract_term": "contract_terms",
}
_STRING_FIELDS = {
    "identity": {
        "title": "title", "reference_number": "reference_number", "rfp_number": "reference_number",
        "tender_number": "reference_number", "solicitation_type": "solicitation_type", "revision": "revision",
        "issue_date": "issue_date", "status": "status", "language": "language", "country": "country",
        "jurisdiction": "jurisdiction",
    },
    "procurement": {
        "procurement_type": "procurement_type", "type": "procurement_type", "method": "method", "category": "category",
        "bid_system": "bid_system", "portal": "portal", "currency": "currency",
    },
    "scope": {
        "summary": "summary", "scope_summary": "summary", "title": "summary", "name": "summary",
        "name_of_job": "summary", "job_name": "summary", "project_title": "summary", "work_title": "summary",
    },
    "bid_rule": {
        "submission_method": "submission_method", "portal": "portal", "bid_structure": "bid_structure",
        "language": "language", "clarification_process": "clarification_process",
    },
    "financial_term": {
        "currency": "currency", "price_basis": "price_basis", "taxes": "taxes", "duties": "duties",
        "price_adjustment": "price_adjustment", "retention": "retention",
        "liquidated_damages": "liquidated_damages",
    },
    "contract_term": {
        key: key for key in (
            "contract_type", "award_basis", "term", "extension", "commencement", "completion", "warranty",
            "support", "termination", "indemnity", "liability", "insurance", "dispute_resolution",
            "governing_law", "jurisdiction", "change_control", "assignment", "subcontracting",
            "confidentiality", "intellectual_property", "force_majeure",
        )
    },
}
_LIST_FIELDS = {
    "scope": {"objectives": "objectives", "locations": "locations", "exclusions": "exclusions"},
    "bid_rule": {
        "currencies": "currencies", "signing_requirements": "signing_requirements",
        "submission_instructions": "submission_instructions",
    },
}
_BOOL_FIELDS = {
    "bid_rule": {
        "alternate_bids_allowed": "alternate_bids_allowed", "consortium_allowed": "consortium_allowed",
        "subcontracting_allowed": "subcontracting_allowed",
    }
}
_INT_FIELDS = {"bid_rule": {"envelope_count": "envelope_count", "validity_days": "validity_days", "bid_validity_days": "validity_days"}}


def _project_simple(out: dict[str, Any], atom: RfpAtom, facts: list[dict[str, Any]]) -> None:
    section = out.setdefault(_SIMPLE_SECTIONS[atom.atom_type], {})
    key = _key(atom.key)
    raw = _raw_value(atom)
    if field := _STRING_FIELDS.get(atom.atom_type, {}).get(key):
        _set_first(section, field, _text(raw))
    elif field := _LIST_FIELDS.get(atom.atom_type, {}).get(key):
        section.setdefault(field, []).extend(_strings(raw))
    elif field := _BOOL_FIELDS.get(atom.atom_type, {}).get(key):
        _set_first(section, field, _bool(raw))
    elif field := _INT_FIELDS.get(atom.atom_type, {}).get(key):
        _set_first(section, field, _nonnegative_int(raw))
    property_aliases = {
        "identity": {
            "ref_no": "reference_number", "reference_no": "reference_number", "reference": "reference_number",
            "rfp_ref": "reference_number", "rfp_reference": "reference_number", "tender_ref": "reference_number",
            "date": "issue_date", "dated": "issue_date", "publication_date": "issue_date",
            "document_title": "title", "rfp_title": "title", "tender_title": "title", "name": "title",
            "type": "solicitation_type", "document_type": "solicitation_type",
        },
        "procurement": {
            "type": "procurement_type", "procurement_method": "method", "bidding_method": "method",
            "bidding_system": "bid_system", "portal_name": "portal", "currency": "currency",
        },
        "scope": {"purpose": "summary", "description": "summary"},
        "bid_rule": {
            "submission_mode": "submission_method", "bid_submission_method": "submission_method",
            "structure": "bid_structure", "envelope_system": "bid_structure", "portal_name": "portal",
        },
        "financial_term": {"price": "price_basis", "pricing_basis": "price_basis"},
        "contract_term": {"support_period": "support"},
    }
    for property_key, value in atom.properties.items():
        if field := property_aliases.get(atom.atom_type, {}).get(_key(property_key)):
            _set_first(section, field, _text(value))
    section.setdefault("facts", []).extend(facts)


def _entity_name(atom: RfpAtom) -> str:
    name = atom.properties.get("name")
    if isinstance(name, str) and name:
        return name
    if atom.actor:
        return atom.actor
    if atom.title:
        return atom.title
    raw = _raw_value(atom)
    return raw if isinstance(raw, str) and raw else atom.key


def _project_party(out: dict[str, Any], atom: RfpAtom, refs: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    section = out.setdefault("parties", {})
    key = _key(atom.key)
    role = atom.properties.get("role")
    role = role if isinstance(role, str) else ""
    if not role:
        role = next((candidate for candidate in (key, _key(atom.category), _key(atom.actor)) if candidate in {"buyer", "issuer", "contact"}), "other")
    if role == "other":
        meaning = _key(" ".join((atom.normalized_text, atom.title, atom.key)))
        if any(marker in meaning for marker in ("issuing_authority", "issuer", "tendering_authority")):
            role = "issuer"
        elif any(marker in meaning for marker in ("buyer", "purchaser", "procuring_entity")):
            role = "buyer"
        elif any(marker in meaning for marker in ("contact", "coordinator", "nodal_officer")):
            role = "contact"
    row: dict[str, Any] = {"party_id": atom.atom_id, "role": role, "name": _entity_name(atom), "source_refs": refs}
    section.setdefault("facts", []).extend(facts)
    for field in ("name", "legal_name", "address", "contact_name", "email", "phone"):
        value = atom.properties.get(field)
        if isinstance(value, str) and value:
            row[field] = value
    if isinstance(atom.properties.get("contact_no"), str):
        row["phone"] = atom.properties["contact_no"]
    if key in {"name", "legal_name", "address", "contact_name", "email", "phone"}:
        row[key] = _text(_raw_value(atom))
    if role in {"buyer", "issuer"} and role not in section:
        section[role] = row
    elif role == "contact":
        section.setdefault("contacts", []).append(row)
    else:
        section.setdefault("other", []).append(row)


def _project_schedule(out: dict[str, Any], atom: RfpAtom, refs: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    section = out.setdefault("schedule", {})
    key = _key(atom.key)
    if key in {"timezone", "calendar"}:
        _set_first(section, key, _text(_raw_value(atom)))
    else:
        properties = atom.properties
        row: dict[str, Any] = {
            "event_id": atom.atom_id,
            "event_type": atom.category or atom.key or "event",
            "name": atom.title or atom.key or atom.normalized_text,
            "mandatory": atom.mandatory,
            "dependency_event_ids": _ids(properties.get("dependency_event_ids")),
            "source_refs": refs,
        }
        for field in ("start", "end", "duration"):
            value = properties.get(field)
            if value not in (None, "", []):
                row[field] = _text(value)
        if key in {"start", "end", "duration"}:
            row[key] = _text(_raw_value(atom))
        elif not any(row.get(field) for field in ("start", "end", "duration")):
            row["start"] = _text(_raw_value(atom))
        section.setdefault("events", []).append(row)
    section.setdefault("facts", []).extend(facts)


def _project_requirement(
    out: dict[str, Any],
    atom: RfpAtom,
    refs: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    required_document_ids: list[str],
) -> None:
    properties = atom.properties
    text = _atom_text(atom)
    if atom.atom_type == "policy" and atom.key:
        policy_name = atom.key.replace("_", " ")
        if "preference to make in india" in policy_name.lower():
            policy_name = f"PPP-MII / {policy_name}"
        text = f"{policy_name}: {text}"
    if atom.atom_type == "annexure":
        probe = _key(" ".join((atom.key, atom.normalized_text, text)))
        if "qualification_criteria" in probe or "eligibility_criteria" in probe:
            text = f"Bidder must satisfy {text}."
        elif "technical_specification" in probe:
            text = f"Bidder must comply with {text}."
    texts = _actionable_requirement_sentences(text) if atom.atom_type == "clause" else [text]
    for index, requirement_text in enumerate(texts, start=1):
        row = {
            "requirement_id": atom.atom_id if len(texts) == 1 else f"{atom.atom_id}:{index}",
            "title": atom.title or atom.key,
            "text": requirement_text,
            "category": atom.category or atom.atom_type,
            "stage": atom.stage,
            "requirement_type": _text(properties.get("requirement_type")) if properties.get("requirement_type") else atom.key,
            "actor": atom.actor or "bidder",
            "action": atom.action,
            "subject": atom.subject,
            "mandatory": atom.mandatory,
            "conditional": bool(atom.condition) if atom.condition else None,
            "condition": atom.condition,
            "exceptions": atom.exceptions,
            "acceptance_criteria": _strings(properties.get("acceptance_criteria")),
            "evidence": atom.evidence_names,
            "failure_effect": atom.failure_effect,
            "clarifiable": _bool(properties.get("clarifiable")),
            "applies_to": _strings(properties.get("applies_to")),
            "lot_ids": _ids(properties.get("lot_ids")),
            "clause_ids": [atom.atom_id],
            "required_document_ids": _unique([*required_document_ids, *_ids(properties.get("required_document_ids"))]),
            "dependency_requirement_ids": _ids(properties.get("dependency_requirement_ids")),
            "source_refs": refs,
            "facts": facts,
        }
        out.setdefault("requirements", []).append(row)


def _actionable_requirement_sentences(text: str) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    actionable = [
        sentence
        for sentence in sentences
        if re.search(
            r"\b(?:must|shall|required\s+to|liable\s+to\s+be\s+rejected|will\s+be\s+rejected|not\s+permitted|prohibited)\b",
            sentence,
            flags=re.IGNORECASE,
        )
    ]
    return actionable or [text]


def _project_evaluation(out: dict[str, Any], atom: RfpAtom, refs: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    section = out.setdefault("evaluation", {})
    key = _key(atom.key)
    if key in {"method", "qualification_rule", "tie_breaker", "normalization"}:
        _set_first(section, key, _text(_raw_value(atom)))
        section.setdefault("facts", []).extend(facts)
        return
    if key in {"stages", "opening_rules"}:
        section.setdefault(key, []).extend(_strings(_raw_value(atom)))
        section.setdefault("facts", []).extend(facts)
        return
    properties = atom.properties
    row: dict[str, Any] = {
        "criterion_id": atom.atom_id,
        "name": atom.title or atom.key or atom.normalized_text,
        "description": _atom_text(atom),
        "stage": atom.stage,
        "mandatory": atom.mandatory,
        "requirement_ids": _ids(properties.get("requirement_ids")),
        "clause_ids": _unique([atom.atom_id, *_ids(properties.get("clause_ids"))]),
        "source_refs": refs,
        "facts": facts,
    }
    for field in ("weight", "maximum_score", "minimum_score"):
        if (value := _number(properties.get(field))) is not None:
            row[field] = value
    if properties.get("formula") not in (None, "", []):
        row["formula"] = _text(properties["formula"])
    section.setdefault("criteria", []).append(row)


def _project_policy(out: dict[str, Any], atom: RfpAtom, refs: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    properties = atom.properties
    row = {
        "policy_id": atom.atom_id,
        "name": _entity_name(atom),
        "reference": _text(properties.get("reference")) if properties.get("reference") else "",
        "applicability": _text(properties.get("applicability")) if properties.get("applicability") else atom.condition,
        "obligations": _strings(properties.get("obligations")) or ([atom.action] if atom.action else []),
        "exemptions": atom.exceptions,
        "source_refs": refs,
        "facts": facts,
    }
    out.setdefault("policies", {}).setdefault("items", []).append(row)


def _project_required_document(
    out: dict[str, Any],
    atom: RfpAtom,
    refs: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    requirement_ids: list[str],
) -> None:
    properties = atom.properties
    row: dict[str, Any] = {
        "required_document_id": atom.atom_id,
        "name": _entity_name(atom),
        "category": atom.category or "other",
        "stage": atom.stage,
        "mandatory": atom.mandatory,
        "condition": atom.condition,
        "requirement_ids": _unique([*requirement_ids, *_ids(properties.get("requirement_ids"))]),
        "annexure_ids": _ids(properties.get("annexure_ids")),
        "source_refs": refs,
        "facts": facts,
    }
    for field in ("format", "issuer", "validity"):
        if properties.get(field) not in (None, "", []):
            row[field] = _text(properties[field])
    for field in ("template_required", "signed", "notarized"):
        if (value := _bool(properties.get(field))) is not None:
            row[field] = value
    if (copies := _nonnegative_int(properties.get("copies"))) is not None:
        row["copies"] = copies
    row["checks"] = _strings(properties.get("checks"))
    out.setdefault("required_documents", []).append(row)


def _project_annexure(out: dict[str, Any], atom: RfpAtom, refs: list[dict[str, Any]], facts: list[dict[str, Any]]) -> None:
    properties = atom.properties
    row = {
        "annexure_id": atom.atom_id,
        "number": _text(properties.get("number")) if properties.get("number") else "",
        "title": atom.title or atom.key,
        "annexure_type": atom.category,
        "availability": _text(properties.get("availability")) if properties.get("availability") else "unknown",
        "required_submission": atom.mandatory if atom.mandatory is not None else _bool(properties.get("required_submission")),
        "description": _atom_text(atom),
        "source_document_ids": _ids(properties.get("source_document_ids")),
        "requirement_ids": _ids(properties.get("requirement_ids")),
        "required_document_ids": _ids(properties.get("required_document_ids")),
        "source_refs": refs,
        "facts": facts,
    }
    out.setdefault("annexures", []).append(row)


_OTHER_FACT_SECTIONS = {
    "identity": "identity", "party": "parties", "parties": "parties", "procurement": "procurement",
    "scope": "scope", "schedule": "schedule", "bid_rule": "bid_rules", "bid_rules": "bid_rules",
    "evaluation": "evaluation", "financial_term": "financial_terms", "financial_terms": "financial_terms",
    "contract_term": "contract_terms", "contract_terms": "contract_terms", "policy": "policies", "policies": "policies",
}


def _promote_core_fields(out: dict[str, Any], atoms: list[RfpAtom]) -> None:
    identity = out.setdefault("identity", {})
    scope = out.setdefault("scope", {})
    for atom in atoms:
        if atom.atom_type != "procurement":
            continue
        probe = _key(" ".join((atom.title, atom.normalized_text, atom.key)))
        if "request_for_proposal" in probe or probe.startswith("rfp_"):
            _set_first(identity, "solicitation_type", "rfp")
            title = atom.subject
            if not title and atom.properties.get("purpose"):
                title = _text(atom.properties["purpose"])
            title = re.sub(r"^(?:rfp|request for proposal)\s+for\s+", "", title, flags=re.IGNORECASE)
            _set_first(identity, "title", title)
        purpose = atom.properties.get("purpose")
        if isinstance(purpose, str) and purpose:
            _set_first(scope, "summary", purpose)
            clean_title = re.sub(r"^(?:rfp|request for proposal)\s+for\s+", "", purpose, flags=re.IGNORECASE)
            _set_first(identity, "title", clean_title)
            duration = atom.properties.get("duration")
            if isinstance(duration, str) and duration:
                _set_first(out.setdefault("contract_terms", {}), "term", duration)
        if "techno_commercial" in probe:
            _set_first(out.setdefault("procurement", {}), "method", "techno-commercial")
        if _key(atom.key) in {"tender_id", "tender_number", "rfp_number", "rfq_no", "crfq_no", "reference_number"}:
            _set_first(identity, "reference_number", _text(_raw_value(atom)))
        if _key(atom.key) in {"title", "solicitation_type"}:
            _set_first(identity, "solicitation_type", _text(_raw_value(atom)))

    for atom in atoms:
        if atom.atom_type != "scope":
            continue
        if _key(atom.key) in {"title", "name", "name_of_job", "job_name", "project_title", "work_title"}:
            value = _text(_raw_value(atom))
            _set_first(scope, "summary", value)
            _set_first(identity, "title", value)

    bid_rules = out.setdefault("bid_rules", {})
    for atom in atoms:
        if atom.atom_type != "bid_rule":
            continue
        probe = _key(" ".join((atom.normalized_text, atom.title, atom.key)))
        procedure = atom.properties.get("procedure")
        if isinstance(procedure, str) and procedure:
            _set_first(bid_rules, "bid_structure", procedure)
        envelope_structure = atom.properties.get("envelope_structure")
        if isinstance(envelope_structure, str) and envelope_structure:
            bid_rules.setdefault("submission_instructions", []).append(envelope_structure)
        parts = atom.properties.get("parts")
        if parts not in (None, "", []):
            bid_rules.setdefault("submission_instructions", []).extend(_strings(parts))
        if "three_envelope" in probe or (isinstance(procedure, str) and "three-envelope" in procedure.lower()):
            _set_first(bid_rules, "envelope_count", 3)
        raw = _text(_raw_value(atom))
        raw_probe = _key(raw)
        if not bid_rules.get("bid_structure") and any(marker in raw_probe for marker in ("two_part", "two_cover", "three_part", "three_cover", "three_envelope")):
            _set_first(bid_rules, "bid_structure", raw)
        if any(marker in f"{probe}_{raw_probe}" for marker in ("two_part", "two_cover")):
            _set_first(bid_rules, "envelope_count", 2)

    parties = out.setdefault("parties", {})
    if not parties.get("buyer") and parties.get("issuer"):
        parties["buyer"] = dict(parties["issuer"])
        parties["buyer"]["role"] = "buyer"
    if not parties.get("issuer") and parties.get("buyer"):
        parties["issuer"] = dict(parties["buyer"])
        parties["issuer"]["role"] = "issuer"


def _is_requirement(atom: RfpAtom) -> bool:
    if atom.atom_type == "requirement" or atom.properties.get("is_requirement") is True:
        return True
    key = _key(" ".join((atom.key, atom.normalized_text)))
    raw = _key(_text(_raw_value(atom)))
    if atom.atom_type == "policy" and raw in {"applicable", "not_applicable", "yes", "no"}:
        return True
    if atom.atom_type == "annexure" and any(
        marker in key
        for marker in ("qualification_criteria", "eligibility_criteria", "technical_specification", "payment_terms")
    ):
        return True
    if atom.atom_type in {"clause", "contract_term", "bid_rule"} and re.search(
        r"\b(?:must|shall|required\s+to|liable\s+to\s+be\s+rejected|will\s+be\s+rejected|not\s+permitted|prohibited)\b",
        _atom_text(atom),
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _is_required_document(atom: RfpAtom) -> bool:
    if atom.atom_type == "required_document" or atom.properties.get("is_required_document") is True:
        return True
    if atom.atom_type != "annexure":
        return False
    name = _key(" ".join((atom.key, atom.title, atom.normalized_text)))
    return any(
        marker in name
        for marker in (
            "bid", "document_upload", "declaration", "undertaking", "certificate", "confirmation",
            "deviation", "integrity_pact", "billing_address", "form", "format", "checklist",
        )
    )


def project_atoms_to_base_rfp(
    batch: Mapping[str, Any] | RfpAtomBatch,
    source_units: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project atoms into a source-grounded partial accepted by Base RFP v1."""
    units = _load_source_units(source_units)
    normalized = normalize_atom_batch(batch, units)
    out: dict[str, Any] = {"clauses": []}

    document_ids_by_name: dict[str, list[str]] = {}
    requirement_ids_by_evidence: dict[str, list[str]] = {}
    for atom in normalized.atoms:
        if _is_required_document(atom) and (name := _entity_name(atom)):
            document_ids_by_name.setdefault(name, []).append(atom.atom_id)
        if _is_requirement(atom):
            for name in atom.evidence_names:
                requirement_ids_by_evidence.setdefault(name, []).append(atom.atom_id)

    for atom in normalized.atoms:
        if atom.atom_type == "non_semantic":
            continue
        refs = _source_refs(atom, units)
        facts = _atom_facts(atom, refs)
        is_requirement = _is_requirement(atom)
        is_required_document = _is_required_document(atom)
        clause: dict[str, Any] = {
            "clause_id": atom.atom_id,
            "number": _text(atom.properties.get("number")) if atom.properties.get("number") else _clause_number(atom, units),
            "title": atom.title or atom.key,
            "text": _atom_text(atom),
            "section_path": _section_path(atom, units),
            "clause_type": atom.atom_type,
            "actor": atom.actor,
            "obligation": " ".join(part for part in (atom.modality, atom.action, atom.subject) if part),
            "mandatory": atom.mandatory,
            "conditions": [atom.condition] if atom.condition else [],
            "exceptions": atom.exceptions,
            "requirement_ids": [atom.atom_id] if is_requirement else [],
            "source_refs": refs,
        }
        out["clauses"].append(clause)

        if atom.atom_type in _SIMPLE_SECTIONS:
            _project_simple(out, atom, facts)
        elif atom.atom_type == "party":
            _project_party(out, atom, refs, facts)
        elif atom.atom_type == "schedule":
            _project_schedule(out, atom, refs, facts)
        elif atom.atom_type == "requirement":
            linked = _unique(doc_id for name in atom.evidence_names for doc_id in document_ids_by_name.get(name, []))
            if is_required_document:
                linked = _unique([*linked, atom.atom_id])
            _project_requirement(out, atom, refs, facts, linked)
        elif atom.atom_type == "evaluation":
            _project_evaluation(out, atom, refs, facts)
        elif atom.atom_type == "policy":
            _project_policy(out, atom, refs, facts)
        elif atom.atom_type == "required_document":
            linked_requirements = requirement_ids_by_evidence.get(_entity_name(atom), [])
            if is_requirement:
                linked_requirements = _unique([*linked_requirements, atom.atom_id])
            _project_required_document(out, atom, refs, facts, linked_requirements)
        elif atom.atom_type == "annexure":
            _project_annexure(out, atom, refs, facts)
        elif atom.atom_type == "definition":
            properties = atom.properties
            out.setdefault("definitions", []).append(
                {
                    "definition_id": atom.atom_id,
                    "term": atom.key or atom.title,
                    "definition": _atom_text(atom),
                    "aliases": _unique([*atom.values, *_strings(properties.get("aliases"))]) if all(isinstance(value, str) for value in atom.values) else _strings(properties.get("aliases")),
                    "source_refs": refs,
                }
            )
            clause["facts"] = facts
        else:
            target = _OTHER_FACT_SECTIONS.get(_key(atom.category)) if atom.atom_type == "other" else None
            if target:
                out.setdefault(target, {}).setdefault("facts", []).extend(facts)
            else:
                clause["facts"] = facts

        if is_requirement and atom.atom_type != "requirement":
            linked = _unique(doc_id for name in atom.evidence_names for doc_id in document_ids_by_name.get(name, []))
            if is_required_document:
                linked = _unique([*linked, atom.atom_id])
            _project_requirement(out, atom, refs, facts, linked)
        if is_required_document and atom.atom_type != "required_document":
            linked_requirements = requirement_ids_by_evidence.get(_entity_name(atom), [])
            if is_requirement:
                linked_requirements = _unique([*linked_requirements, atom.atom_id])
            _project_required_document(out, atom, refs, facts, linked_requirements)

    _promote_core_fields(out, normalized.atoms)
    normalize_base_rfp_partial(out)
    return out


def _self_check() -> None:
    requirement = {
        "atom_id": "a-requirement",
        "atom_type": "requirement",
        "key": "bid_security",
        "title": "Bid security",
        "normalized_text": "The bidder shall submit bid security.",
        "actor": "bidder",
        "action": "submit",
        "subject": "bid security",
        "modality": "shall",
        "mandatory": True,
        "category": "submission",
        "stage": "bid_submission",
        "failure_effect": "rejection",
        "evidence_names": ["Bid Security Form"],
        "source_unit_ids": ["u1"],
    }
    financial = {
        "atom_id": "a-financial",
        "atom_type": "financial_term",
        "key": "price_basis",
        "title": "Price basis",
        "normalized_text": "Prices shall remain firm.",
        "value": "firm",
        "properties": {"escalation_formula": "not applicable"},
        "source_unit_ids": ["u2"],
    }
    units = {
        "u1": {"text": "The bidder shall submit bid security.", "document": "rfp.pdf", "page": 2, "page_id": "p2", "block": "b1", "section_path": ["Bid security"]},
        "u2": {"text": "Prices shall remain firm.", "document": "rfp.pdf", "page": 3, "page_id": "p3", "cell": "c7", "section_path": ["Commercial terms"]},
    }
    batch = normalize_atom_batch({"atoms": [requirement, requirement, financial]}, units)
    assert len(batch.atoms) == 2
    partial = project_atoms_to_base_rfp(batch, units)
    assert partial["clauses"][0]["source_refs"][0]["quote"] == units["u1"]["text"]
    assert partial["requirements"][0]["clause_ids"] == ["a-requirement"]
    assert any(fact["key"] == "escalation_formula" for fact in partial["financial_terms"]["facts"])
    record = create_base_rfp_understanding(partial)
    assert len(record.clauses) == 2 and len(record.requirements) == 1
    requirement_row = record.requirements[0]
    clause_row = next(row for row in record.clauses if row.title == "Bid security")
    assert requirement_row.clause_ids == [clause_row.clause_id]
    assert clause_row.requirement_ids == [requirement_row.requirement_id]
    try:
        normalize_atom_batch({"atoms": [{**requirement, "source_unit_ids": ["missing"]}]}, units)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("unknown source unit ID was accepted")


if __name__ == "__main__":
    _self_check()
