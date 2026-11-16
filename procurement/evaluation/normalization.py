"""Normalize provider evaluation payloads before schema validation."""

from __future__ import annotations

import json
from typing import Any

from procurement.domain.schemas import Criterion, TenderProfile
from procurement.domain.serialization import model_dump


def normalize_evaluation_data(data: dict[str, Any]) -> None:
    tender = data.get("tender")
    if isinstance(tender, dict):
        normalize_tender_profile_data(tender)


def normalize_tender_profile_data(data: dict[str, Any]) -> None:
    source_notes = data.get("source_notes")
    if isinstance(source_notes, dict):
        data["source_notes"] = [
            f"{key}: {value}" if value not in (None, "") else str(key)
            for key, value in source_notes.items()
        ]
    criteria = data.get("criteria")
    if isinstance(criteria, list):
        for criterion in criteria:
            if isinstance(criterion, dict) and is_post_award_criterion_data(criterion):
                criterion["mandatory"] = False
                criterion["failure_effect"] = "no_direct_fail"
                criterion["priority"] = "note"


def is_post_award_criterion(criterion: Criterion) -> bool:
    return is_post_award_criterion_data(model_dump(criterion))


def is_post_award_criterion_data(criterion: dict[str, Any]) -> bool:
    kind = str(criterion.get("criterion_kind") or "").strip().lower().replace("-", "_").replace(" ", "_")
    scope = str(criterion.get("evaluation_scope") or "").strip().lower().replace("-", "_").replace(" ", "_")
    stage = str(criterion.get("stage") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if kind == "post_award" or scope in {"post_award", "commercial_out_of_scope"} or stage == "contract":
        return True
    text = " ".join(
        stringify_profile_value(criterion.get(key))
        for key in ["stage", "applies_when", "requirement", "pass_condition", "source_quote", "priority"]
    ).lower()
    strong_post_award = any(
        phrase in text
        for phrase in [
            "post-award",
            "post award",
            "after award",
            "after placement of order",
            "after purchase order",
            "supply order invoice",
            "invoice uploading",
            "uploading tax invoice",
            "digitally signed tax invoice",
            "inspection call",
        ]
    )
    if strong_post_award:
        return True
    if kind or scope:
        return False
    return "successful bidder" in text


def normalize_vendor_profile_data(data: dict[str, Any]) -> None:
    vendor_name = stringify_profile_value(data.get("vendor_name")).strip()
    data["vendor_name"] = vendor_name or "Unknown"
    for key in ["identifiers", "contact", "financials", "commercial_offer"]:
        value = data.get(key)
        if isinstance(value, dict):
            data[key] = {str(k): stringify_profile_value(v) for k, v in value.items()}
        elif value in (None, ""):
            data[key] = {}
    for key in ["documents_submitted", "claimed_experience", "declarations", "extraction_warnings"]:
        value = data.get(key)
        if isinstance(value, list):
            data[key] = [stringify_profile_value(item) for item in value]
        elif value in (None, ""):
            data[key] = []
        else:
            data[key] = [stringify_profile_value(value)]


def stringify_profile_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True)
