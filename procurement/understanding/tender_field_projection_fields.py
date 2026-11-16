"""Field collection, status, summary, and profile projections."""

from __future__ import annotations

import re
from typing import Any

from .tender_field_projection_provenance import (
    _canonical_source_references,
    _canonical_source_units,
)


def _canonicalize_field_evidence(
    understanding: dict[str, Any], source_units: dict[str, dict[str, Any]]
) -> None:
    canonical_units = _canonical_source_units(source_units)
    for field in field_entries(understanding):
        evidence = _canonical_source_references(field.get("evidence") or [], canonical_units)
        if evidence:
            field["evidence"] = evidence


def _restore_serialized_collection_values(understanding: dict[str, Any]) -> None:
    """Restore a provider list that an older post-processor serialized as text."""
    for field in field_entries(understanding):
        value = field.get("value")
        if not (
            isinstance(value, str)
            and value.strip().startswith("[")
            and value.strip().endswith("]")
        ):
            continue
        rounds = (field.get("audit") or {}).get("normalized_rounds") or []
        for round_data in reversed(rounds):
            raw_value = ((round_data or {}).get("raw") or {}).get("value")
            if not (
                isinstance(raw_value, list)
                and raw_value
                and all(isinstance(item, str) and item.strip() for item in raw_value)
            ):
                continue
            field["value"] = [" ".join(item.split()) for item in raw_value]
            field.setdefault("audit", {})["serialized_collection_restored"] = True
            break


def field_entries(understanding: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        field
        for section in understanding.get("sections") or []
        for field in section.get("fields") or []
        if isinstance(field, dict)
    ]


def _refresh_understanding_status(understanding: dict[str, Any]) -> None:
    fields = field_entries(understanding)
    counts = {
        status: sum(field.get("status") == status for field in fields)
        for status in (
            "found",
            "not_found",
            "not_applicable",
            "ambiguous",
            "need_more_evidence",
            "unresolved",
        )
    }
    unverified = sum(field.get("audit", {}).get("verified") is not True for field in fields)
    understanding["status"] = (
        "complete"
        if counts["ambiguous"] + counts["unresolved"] == 0 and unverified == 0
        else "partial"
        if counts["found"]
        else "failed"
    )
    understanding.setdefault("audit", {}).update(
        fields_total=len(fields),
        fields_found=counts["found"],
        fields_not_found=counts["not_found"],
        fields_not_applicable=counts["not_applicable"],
        fields_ambiguous=counts["ambiguous"],
        fields_need_more_evidence=counts["need_more_evidence"],
        fields_unresolved=counts["unresolved"],
        fields_unverified=unverified,
    )


def summary_from_resolved_fields(understanding: dict[str, Any]) -> str:
    fields = {
        str(item.get("key") or ""): item.get("value")
        for item in field_entries(understanding)
        if item.get("status") == "found"
    }

    def compact(value: Any, limit: int = 2) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value[:limit] if item not in (None, ""))
        return str(value or "").strip()

    def named(value: Any, *labels: str) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if any(label in str(key).replace("_", " ").casefold() for label in labels):
                    return compact(item)
        if isinstance(value, list):
            for item in value:
                key, separator, content = str(item).partition(":")
                if separator and any(label in key.casefold() for label in labels):
                    content = content.strip()
                    if content.casefold() not in {
                        "(not specified)",
                        "not specified",
                        "not found",
                        "unknown",
                    }:
                        return content
        return ""

    identity = fields.get("tender_identity")
    subject = fields.get("tender_subject_classification")
    tender_number = named(identity, "gem bid number", "bid number", "tender number")
    buyer = named(identity, "organisation", "organization", "buyer", "issuing authority")
    title = named(subject, "title", "name of work")
    if not any((tender_number, buyer, title)):
        return ""

    if buyer and title and tender_number:
        sentences = [f"{buyer} issued tender {tender_number} for {title}."]
    elif title and tender_number:
        sentences = [f"Tender {tender_number} is for {title}."]
    elif buyer and title:
        sentences = [f"{buyer} issued a tender for {title}."]
    elif title:
        sentences = [f"This tender is for {title}."]
    else:
        sentences = [f"Tender {tender_number} was issued by {buyer}.".strip()]

    estimated = fields.get("estimated_procurement_value")
    value = named(estimated, "estimated value", "estimated cost", "amount") or compact(estimated, 1)
    security = fields.get("bid_security_and_fees") or fields.get("bid_security_fees")
    emd = named(security, "emd", "bid security")
    fee = named(security, "tender fee", "document fee", "processing fee")
    commercial = [
        text
        for text in (
            f"estimated value {value}" if value else "",
            f"EMD {emd}" if emd else "",
            f"tender fee {fee}" if fee else "",
        )
        if text
    ]
    if commercial:
        sentences.append("Key commercial terms are " + ", ".join(commercial) + ".")

    timeline = fields.get("tender_timeline")
    deadline = named(timeline, "submission deadline", "submission end", "closing date")
    opening = named(timeline, "technical opening", "bid opening", "financial opening")
    if deadline or opening:
        text = f"Bid submission closes on {deadline}" if deadline else ""
        if opening:
            text += (", with bid opening on " if text else "Bid opening is on ") + opening
        sentences.append(text + ".")

    locations = named(fields.get("work_delivery_location"), "delivery locations", "locations", "work location")
    if locations:
        sentences.append(f"Primary delivery or work locations are {locations}.")

    evaluation = fields.get("evaluation_method")
    method = named(evaluation, "method", "evaluation method").rstrip(" .;")
    award = named(evaluation, "award basis", "selection method").rstrip(" .;")
    if method or award:
        sentences.append("Evaluation uses " + " with ".join(text for text in (method, award) if text) + ".")

    policies = []
    if fields.get("mse_startup_policy"):
        policies.append("MSE/startup provisions")
    if fields.get("make_in_india_policy"):
        policies.append("Make in India preference")
    if policies:
        sentences.append("The tender includes " + " and ".join(policies) + ".")
    return " ".join(sentences)


def _project_resolved_tender_summary(understanding: dict[str, Any]) -> None:
    """Build the summary from reviewed fields without discarding provenance."""
    summary = summary_from_resolved_fields(understanding)
    if not summary:
        return
    fields = {
        str(field.get("key") or ""): field
        for field in field_entries(understanding)
    }
    summary_field = fields.get("tender_summary")
    if not summary_field:
        return
    contributor_keys = (
        "tender_identity",
        "tender_subject_classification",
        "estimated_procurement_value",
        "bid_security_and_fees",
        "tender_timeline",
        "work_delivery_location",
        "evaluation_method",
        "mse_startup_policy",
        "make_in_india_policy",
    )
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    used_fields: list[str] = []
    for key in contributor_keys:
        field = fields.get(key) or {}
        if field.get("status") != "found":
            continue
        used_fields.append(key)
        for item in field.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            marker = (
                str(item.get("source_id") or ""),
                str(item.get("page_id") or ""),
                str(item.get("quote") or ""),
            )
            if marker in seen:
                continue
            seen.add(marker)
            evidence.append(dict(item))
            if len(evidence) >= 12:
                break
        if len(evidence) >= 12:
            break
    critical_fields = [fields.get(key) or {} for key in ("tender_identity", "tender_subject_classification")]

    def source_field_verified(field: dict[str, Any]) -> bool:
        audit = field.get("audit") or {}
        return bool(
            field.get("status") == "found"
            and (
                audit.get("verified") is True
                or audit.get("canonical_gem_projection") is True
                or audit.get("deterministic_issuer_projection") is True
            )
        )

    verified = bool(evidence) and all(source_field_verified(field) for field in critical_fields)
    summary_field.update(
        status="found",
        value=summary,
        evidence=evidence,
        confidence=min(
            [
                float((fields.get(key) or {}).get("confidence") or 1.0)
                for key in used_fields
            ]
            or [1.0]
        ),
        notes=(
            ""
            if verified
            else "Summary is derived from resolved fields but one or more identity sources still need review."
        ),
    )
    summary_field["audit"] = {
        **(summary_field.get("audit") or {}),
        "verified": verified,
        "summary_projection": True,
        "summary_source_fields": used_fields,
        "completeness_warnings": (
            []
            if verified
            else ["summary inherits an unverified identity or subject field"]
        ),
        "blocking_grounding_issues": (
            [] if evidence else ["summary has no source evidence"]
        ),
    }


def profile_from_fields(understanding: dict[str, Any]) -> dict[str, Any]:
    fields = {str(item.get("key") or ""): item for item in field_entries(understanding)}

    def value(key: str, default: Any = "") -> Any:
        item = fields.get(key) or {}
        return item.get("value") if item.get("status") == "found" else default

    def as_text(item: Any, default: str = "") -> str:
        if isinstance(item, dict):
            return "; ".join(
                f"{str(key).replace('_', ' ')}: {as_text(content)}"
                for key, content in item.items()
                if content not in (None, "", [])
            ) or default
        if isinstance(item, list):
            return ", ".join(str(content) for content in item if content not in (None, "")) or default
        return str(item).strip() if item not in (None, "") else default

    def grouped_value(key: str, labels: tuple[str, ...], default: str) -> str:
        items = value(key, [])
        if isinstance(items, dict):
            candidates = [
                (str(label).replace("_", " ").casefold(), content)
                for label, content in items.items()
            ]
        elif isinstance(items, list):
            candidates = []
            for item in items:
                label, separator, content = str(item).partition(":")
                if separator:
                    candidates.append((label.casefold(), content))
        else:
            return as_text(items, default)
        for name in labels:
            for label, content in candidates:
                if name in label:
                    return as_text(content, default)
        return default

    def bid_system_value() -> str:
        items = value("bid_structure_and_auction", [])
        if isinstance(items, dict):
            candidates = [f"{label}: {content}" for label, content in items.items()]
        elif isinstance(items, list):
            candidates = [str(item) for item in items]
        else:
            candidates = [str(items)]
        for candidate in candidates:
            text = candidate.casefold()
            if any(marker in text for marker in ("three packet", "three-part", "three part", "three cover")):
                return "Three Part Bid"
            if any(marker in text for marker in ("two packet", "two-part", "two part", "two cover")):
                return "Two Part Bid"
            if any(marker in text for marker in ("single packet", "single-part", "single part", "single cover")):
                return "Single Part Bid"
        return "Unknown"

    category = as_text(value("product_category", ""), "")
    return {
        "tender_title": as_text(value("tender_title", ""), "") or grouped_value(
            "tender_subject_classification", ("title", "name of work"), "Unknown"
        ),
        "tender_number": as_text(value("tender_id", ""), "") or grouped_value(
            "tender_identity", ("gem bid number", "bid number", "tender number"), ""
        ),
        "buyer": as_text(value("organisation", ""), "") or grouped_value(
            "tender_identity", ("organisation", "organization", "buyer", "issuing authority"), "Unknown"
        ),
        "procurement_type": category or as_text(value("tender_type", ""), "") or grouped_value(
            "tender_subject_classification",
            ("procurement category", "procurement type", "item category", "nature of work"),
            "Unknown",
        ),
        "bid_system": as_text(value("bidding_system", ""), "") or bid_system_value(),
        "scope": as_text(value("tender_summary", ""), ""),
        "evaluation_method": grouped_value(
            "evaluation_method", ("evaluation method", "method", "award basis"), ""
        ),
        "mandatory_documents": sorted(
            {
                str(document)
                for field in fields.values()
                if field.get("status") == "found"
                for document in field.get("required_documents") or []
                if str(document).strip()
            }
        ),
        "criteria": [],
        "source_notes": [],
    }
