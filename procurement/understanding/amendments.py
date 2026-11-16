"""Tender amendment application and package-completeness checks."""

from __future__ import annotations

import ast
from functools import lru_cache
import hashlib
import json
import os
import re
from typing import Any

from base_rfp import BaseRfpAmendmentEvent, BaseRfpUnderstanding
from procurement.domain.schemas import ExtractedDocument, TenderProfile
from procurement.domain.text import clean_text, normalize_for_match


def apply_tender_amendments(tender: TenderProfile, tender_docs: list[ExtractedDocument]) -> None:
    replacements = amendment_replacements(tender_docs)
    if not replacements:
        return
    for old, new in replacements:
        for criterion in tender.criteria:
            criterion.requirement = replace_case_insensitive(criterion.requirement, old, new)
            criterion.pass_condition = replace_case_insensitive(criterion.pass_condition, old, new)
            criterion.source_quote = replace_case_insensitive(criterion.source_quote, old, new) if criterion.source_quote else criterion.source_quote
            criterion.required_documents = [replace_case_insensitive(document, old, new) for document in criterion.required_documents]
        tender.source_notes.append(f"Applied tender amendment: {old} -> {new}")


def amendment_replacements(tender_docs: list[ExtractedDocument]) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for document in tender_docs:
        text = document.text
        classification = document.record.classification
        haystack = f"{document.record.file_name}\n{text[:8000]}".lower()
        if not (
            "corrigendum" in haystack
            or "addendum" in haystack
            or "pre-bid" in haystack
            or (classification and classification.document_type in {"corrigendum", "prebid_clarification"})
        ):
            continue
        for old, new in re.findall(
            r"\b(Model\s+[A-Z0-9-]+)\b.{0,120}?\b(?:changed|replaced|amended|modified)\b.{0,80}?\b(?:to|by|with)\s+\b(Model\s+[A-Z0-9-]+)\b",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            if old.lower() != new.lower():
                replacements.append((clean_text(old), clean_text(new)))
    return replacements


def replace_case_insensitive(text: str, old: str, new: str) -> str:
    return re.sub(re.escape(old), new, text, flags=re.IGNORECASE)


def tender_completeness_warnings(tender_docs: list[ExtractedDocument], tender: TenderProfile) -> list[str]:
    warnings: list[str] = []
    all_text = "\n".join(doc.text[:10000] for doc in tender_docs).lower()
    doc_types = {
        doc.record.classification.document_type
        for doc in tender_docs
        if doc.record.classification
    }
    looks_like_gem_base = "gem/" in all_text or "gem-bidding" in all_text or "bid number" in all_text or "bid details" in all_text
    has_detail_doc = bool(doc_types & {"atc_stc_sla_gtc", "technical_specs_or_sow", "eligibility_pqc", "boq_or_price_schedule"})
    mentions_missing_attachment = any(
        phrase in all_text
        for phrase in [
            "buyer added bid specific atc",
            "additional terms and conditions attached",
            "commercial details attached",
            "pqc attached",
            "technical specification attached",
            "boq attached",
        ]
    )
    if looks_like_gem_base and mentions_missing_attachment and not has_detail_doc:
        warnings.append("Tender packet may be incomplete: base GeM bid references ATC/PQC/specification/commercial attachments that were not uploaded.")
    if not tender.criteria:
        warnings.append("Tender criteria could not be extracted from the uploaded tender packet.")
    if tender_has_unapplied_amendment(tender_docs, tender):
        message = "Tender amendment/corrigendum is present but could not be applied deterministically; manual review is required."
        warnings.append(message)
        tender.source_notes.append(message)
    return warnings


def tender_has_unapplied_amendment(tender_docs: list[ExtractedDocument], tender: TenderProfile) -> bool:
    if any("Applied tender amendment:" in note for note in tender.source_notes):
        return False
    for document in tender_docs:
        classification = document.record.classification
        haystack = f"{document.record.file_name}\n{document.text[:2000]}".lower()
        if classification and classification.document_type in {"corrigendum", "prebid_clarification"}:
            return True
        if re.search(r"\b(corrigendum|addendum|pre-bid clarification)\s*[:/]", haystack):
            return True
    return False


BASE_RFP_AMENDMENT_TARGETS_PER_CHUNK = max(10, int(os.getenv("BASE_RFP_AMENDMENT_TARGETS_PER_CHUNK", "40")))


def base_rfp_amendment_target_catalog(record: BaseRfpUnderstanding) -> list[dict[str, Any]]:
    data = record.model_dump(mode="json")
    targets: list[dict[str, Any]] = []

    def add(path: str, value: Any, target_id: str = "", label: str = "", *, allow_blank: bool = False) -> None:
        if not allow_blank and value in (None, "", [], {}):
            return
        targets.append(
            {
                "candidate_id": f"T{len(targets) + 1}",
                "target_path": path,
                "target_id": target_id,
                "target_text": value if isinstance(value, str) else "",
                "current_value": value,
                "label": label,
            }
        )

    for section_name in ("identity", "procurement", "scope", "bid_rules", "financial_terms", "contract_terms"):
        section = data.get(section_name) or {}
        for field, value in section.items():
            if isinstance(value, (str, int, float, bool)):
                add(f"{section_name}.{field}", value, label=f"{section_name}.{field}")

    for party_role in ("buyer", "issuer"):
        party = (data.get("parties") or {}).get(party_role) or {}
        for field in ("name", "legal_name", "address", "contact_name", "email", "phone"):
            if isinstance(party.get(field), str):
                add(f"parties.{party_role}.{field}", party[field], label=f"{party_role} {party.get('name') or ''}")

    collections = (
        ("clauses", "clause_id", ("number", "title", "text", "status")),
        ("requirements", "requirement_id", ("title", "text", "mandatory", "condition", "failure_effect", "status")),
        ("schedule.events", "event_id", ("event_type", "name", "start", "end", "duration", "mandatory")),
        ("evaluation.criteria", "criterion_id", ("name", "description", "stage", "weight", "maximum_score", "minimum_score", "formula", "mandatory")),
        ("required_documents", "required_document_id", ("name", "mandatory", "condition", "format", "validity", "copies")),
        ("annexures", "annexure_id", ("number", "title", "availability", "required_submission", "description")),
        ("definitions", "definition_id", ("term", "definition")),
        ("policies.items", "policy_id", ("name", "reference", "applicability")),
        ("parties.contacts", "party_id", ("name", "contact_name", "email", "phone", "address")),
        ("parties.other", "party_id", ("name", "contact_name", "email", "phone", "address")),
    )
    for path, id_field, fields in collections:
        current: Any = data
        for part in path.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        for item in current or []:
            if not isinstance(item, dict) or not item.get(id_field):
                continue
            label = str(item.get("title") or item.get("name") or item.get("term") or item.get("text") or "")
            for field in fields:
                if isinstance(item.get(field), (str, int, float, bool)):
                    add(
                        f"{path}.{field}",
                        item[field],
                        str(item[id_field]),
                        label,
                        allow_blank=path == "schedule.events" and field in {"start", "end"},
                    )
    return targets


AMENDMENT_MATCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "shall", "will", "are", "was", "were",
    "existing", "revised", "change", "changed", "amended", "corrigendum", "addendum", "rfp", "tender",
}


def amendment_match_terms(value: Any) -> set[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 2 and token not in AMENDMENT_MATCH_STOPWORDS
    }


def base_rfp_amendment_targets_for_units(
    catalog: list[dict[str, Any]],
    source_units: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    source_text = "\n".join(str(unit.get("text") or "") for unit in source_units.values())
    source_terms = amendment_match_terms(source_text)
    normalized_source = normalize_for_match(source_text)
    ranked = []
    for target in catalog:
        probe = " ".join((target["target_path"], target.get("label") or "", str(target.get("current_value") or "")))
        terms = amendment_match_terms(probe)
        overlap = len(source_terms & terms)
        current = normalize_for_match(str(target.get("current_value") or ""))
        exact = bool(len(current) >= 8 and current in normalized_source)
        score = overlap * 4 + (30 if exact else 0)
        ranked.append((score, exact, target))
    ranked.sort(key=lambda item: (-item[0], not item[1], item[2]["candidate_id"]))
    return [target for _score, _exact, target in ranked[:BASE_RFP_AMENDMENT_TARGETS_PER_CHUNK]]


def base_rfp_amendment_source_batches(
    source_units: dict[str, dict[str, Any]],
    max_units: int = 8,
    max_chars: int = 5000,
) -> list[dict[str, dict[str, Any]]]:
    batches: list[dict[str, dict[str, Any]]] = []
    current: dict[str, dict[str, Any]] = {}
    characters = 0
    for unit_id, unit in source_units.items():
        size = len(str(unit.get("text") or ""))
        if current and (len(current) >= max_units or characters + size > max_chars):
            batches.append(current)
            current = {}
            characters = 0
        current[unit_id] = unit
        characters += size
    if current:
        batches.append(current)
    return batches


@lru_cache(maxsize=1)
def base_rfp_amendment_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["add", "replace", "delete", "restate", "clarify"]},
                        "target_candidate_id": {"type": "string"},
                        "target_path": {"type": "string"},
                        "target_hint": {"type": "string"},
                        "previous_value_json": {"type": "string"},
                        "value_json": {"type": "string"},
                        "issued_at": {"type": "string"},
                        "effective_at": {"type": "string"},
                        "resolution_status": {"type": "string", "enum": ["unresolved", "reviewed", "resolved", "rejected"]},
                        "effective_status": {"type": "string", "enum": ["pending", "effective", "superseded", "withdrawn"]},
                        "confidence": {"type": "number"},
                        "source_unit_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "operation", "target_path", "target_hint", "previous_value_json", "value_json", "issued_at", "effective_at",
                        "resolution_status", "effective_status", "confidence", "source_unit_ids",
                    ],
                },
            }
        },
        "required": ["events"],
    }


def resolve_base_rfp_amendment_candidate(
    raw: dict[str, Any],
    source_units: dict[str, dict[str, Any]],
    source_ids: list[str],
    targets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    target_by_id = {target["candidate_id"]: target for target in targets}
    supplied = target_by_id.get(str(raw.get("target_candidate_id") or ""))
    if supplied is not None:
        return supplied

    source_text = "\n".join(str(source_units[unit_id].get("text") or "") for unit_id in source_ids)
    previous = str(raw.get("previous_value_json") or "")
    hint = str(raw.get("target_hint") or "")
    path_hint = str(raw.get("target_path") or "")
    query = "\n".join((source_text, previous, hint, path_hint))
    normalized_query = normalize_for_match(query)

    exact: list[dict[str, Any]] = []
    for target in targets:
        current = normalize_for_match(str(target.get("current_value") or ""))
        label = normalize_for_match(str(target.get("label") or ""))
        if (len(current) >= 8 and current in normalized_query) or (len(label) >= 8 and label in normalized_query):
            exact.append(target)
    if re.search(r"\b(?:date|time|hours?|hrs?|deadline|submission)\b", query, re.IGNORECASE):
        dated = [target for target in exact if target["target_path"].endswith((".start", ".end"))]
        exact = dated or exact
    if len(exact) == 1:
        return exact[0]

    query_terms = amendment_match_terms(query)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for target in targets:
        target_path = str(target.get("target_path") or "")
        probe = " ".join(
            (target_path, str(target.get("label") or ""), str(target.get("current_value") or ""))
        )
        overlap = len(query_terms & amendment_match_terms(probe))
        score = overlap * 4
        if path_hint and (target_path == path_hint or target_path.startswith(path_hint + ".")):
            score += 16
        normalized_label = normalize_for_match(str(target.get("label") or ""))
        if len(normalized_label) >= 8 and normalized_label in normalize_for_match(hint):
            score += 24
        ranked.append((score, target))
    ranked.sort(key=lambda item: (-item[0], item[1]["candidate_id"]))
    if not ranked or ranked[0][0] < 16:
        return None
    runner_up = ranked[1][0] if len(ranked) > 1 else 0
    return ranked[0][1] if ranked[0][0] - runner_up >= 8 else None


def normalize_base_rfp_amendment_response(
    data: Any,
    source_units: dict[str, dict[str, Any]],
    targets: list[dict[str, Any]],
) -> tuple[list[BaseRfpAmendmentEvent], list[str]]:
    target_by_id = {target["candidate_id"]: target for target in targets}
    allowed_add_paths = {"clauses", "requirements", "schedule.events", "evaluation.criteria", "required_documents", "annexures", "definitions", "policies.items"}
    events: list[BaseRfpAmendmentEvent] = []
    warnings: list[str] = []
    for index, raw in enumerate(data.get("events") if isinstance(data, dict) else [], start=1):
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "")
        sources = [str(unit_id) for unit_id in raw.get("source_unit_ids") or [] if str(unit_id) in source_units]
        if operation not in {"add", "replace", "delete", "restate", "clarify"}:
            warnings.append(f"Ignored malformed amendment event {index}: operation is invalid.")
            continue
        if not sources:
            sources = list(source_units)
        candidate = resolve_base_rfp_amendment_candidate(raw, source_units, sources, targets)
        if candidate is None and operation != "add":
            source_text = normalize_for_match("\n".join(source_units[unit_id]["text"] for unit_id in sources))
            exact_labels = [
                target
                for target in targets
                if len(normalize_for_match(str(target.get("label") or ""))) >= 8
                and normalize_for_match(str(target.get("label") or "")) in source_text
            ]
            if re.search(r"\b(?:date|time|hours?|hrs?)\b", str(raw.get("value_json") or ""), re.IGNORECASE):
                dated = [target for target in exact_labels if target["target_path"].endswith(".start")]
                exact_labels = dated or exact_labels
            if len(exact_labels) == 1:
                candidate = exact_labels[0]
        resolution = str(raw.get("resolution_status") or "unresolved")
        target_path = str(raw.get("target_path") or "unresolved")
        target_id = ""
        target_text = ""
        if operation == "add":
            resolution = "resolved" if target_path in allowed_add_paths else "unresolved"
        elif candidate:
            target_path = candidate["target_path"]
            target_id = candidate["target_id"]
            resolution = "resolved"
        else:
            resolution = "unresolved"
            target_path = "unresolved"

        value: Any = None
        if operation != "delete":
            raw_value = str(raw.get("value_json") or "")
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                if raw_value and not raw_value.lstrip().startswith(("{", "[")):
                    value = raw_value
                else:
                    try:
                        value = ast.literal_eval(raw_value)
                    except (SyntaxError, ValueError):
                        warnings.append(f"Ignored amendment event {index}: value_json is invalid.")
                        continue
            if candidate and isinstance(candidate.get("current_value"), str) and isinstance(value, dict):
                if value.get("date") or value.get("time"):
                    value = " ".join(str(value.get(field) or "") for field in ("date", "time")).strip()
            if candidate and json.dumps(value, ensure_ascii=True, sort_keys=True, default=str) == json.dumps(
                candidate.get("current_value"), ensure_ascii=True, sort_keys=True, default=str
            ):
                continue
        refs = [
            {
                "document": source_units[unit_id]["document"],
                "page": source_units[unit_id]["page"],
                "page_id": source_units[unit_id]["page_id"],
                "block": source_units[unit_id].get("block") or "",
                "table": source_units[unit_id].get("table") or "",
                "cell": source_units[unit_id].get("cell") or "",
                "quote": source_units[unit_id]["text"],
            }
            for unit_id in sources
        ]
        payload = {
            "operation": operation,
            "target_path": target_path,
            "target_id": target_id,
            "target_text": target_text,
            "issued_at": clean_text(str(raw.get("issued_at") or "")),
            "effective_at": clean_text(str(raw.get("effective_at") or "")),
            "resolution_status": resolution if resolution in {"unresolved", "reviewed", "resolved", "rejected"} else "unresolved",
            "effective_status": str(raw.get("effective_status") or "pending"),
            "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0.0))),
            "source_refs": refs,
        }
        if operation != "delete":
            payload["value"] = value
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest()[:16]
        payload["event_id"] = f"amend_{fingerprint}"
        try:
            events.append(BaseRfpAmendmentEvent.model_validate(payload))
        except Exception as exc:
            warnings.append(f"Ignored amendment event {index}: {str(exc)[:300]}")
    return events, warnings
