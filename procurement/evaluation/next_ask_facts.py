"""Deterministic finding facts used by the bid policy next-action layer."""

from __future__ import annotations

import re
from typing import Any

from procurement.compliance.corpus import normalize_ws
from procurement.evaluation.decision import canonical_criterion_kind


_PASS = {
    "pass",
    "passed",
    "qualified",
    "compliant",
    "met",
    "not_evaluated",
    "not_applicable",
}

_POST_AWARD_PHRASES = (
    "post award",
    "after award",
    "after placement of order",
    "after purchase order",
    "supply order invoice",
    "invoice uploading",
    "uploading tax invoice",
    "digitally signed tax invoice",
    "inspection call",
)

PAN_RE = re.compile(r"\bPAN\s+([A-Z]{5}\d{4}[A-Z])\b", re.I)
GSTIN_RE = re.compile(r"\bGSTIN\s+([0-9A-Z]{15})\b", re.I)
LOCAL_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*local content", re.I)
IDENTITY_RE = re.compile(
    r"legal identity(?:\s+for)?\s+([A-Z][A-Za-z0-9&.,'() /-]{3,80}?)(?:\s+is\s+established)",
    re.I,
)


def _is_post_award_finding(finding: dict[str, Any]) -> bool:
    kind = canonical_criterion_kind(
        finding.get("criterion_kind"),
        finding.get("materiality"),
        finding.get("decision_effect"),
    )
    scope = str(finding.get("evaluation_scope") or "").casefold()
    if scope:
        return kind == "post_award" or scope in {
            "post_award",
            "commercial_out_of_scope",
            "delivery_or_inspection",
        }
    if kind == "post_award":
        return True
    text = normalize_ws(
        " ".join(
            str(finding.get(key) or "")
            for key in ("criterion_text", "requirement", "reason")
        )
    ).casefold()
    return any(phrase in text for phrase in _POST_AWARD_PHRASES)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def _hay(finding: dict[str, Any]) -> str:
    parts = [
        finding.get("criterion_id"),
        finding.get("criterion_text"),
        finding.get("category"),
        " ".join(finding.get("required_documents") or []),
        " ".join(finding.get("missing_documents") or []),
        finding.get("reason"),
    ]
    return " ".join(str(part or "") for part in parts).casefold()


def _cid(finding: dict[str, Any]) -> str:
    return str(finding.get("criterion_id") or "")


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = normalize_ws(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _reason_says_present(reason: str, paper: str) -> bool:
    blob = f"{reason} {paper}".casefold()
    if "land border" in paper.casefold() and "land border" in reason.casefold():
        return any(
            word in reason.casefold()
            for word in ("provided", "submitted", "found on page", "undertaking was provided")
        )
    if "provided" in blob and paper.casefold()[:18] in reason.casefold():
        return "not provided" not in reason.casefold() and "not submitted" not in reason.casefold()
    return False


def missing_papers(finding: dict[str, Any]) -> list[str]:
    reason = str(finding.get("reason") or "")
    listed = [
        str(item).strip()
        for item in (finding.get("missing_documents") or finding.get("required_documents") or [])
        if str(item).strip()
    ]
    kept = [item for item in listed if not _reason_says_present(reason, item)]
    return _unique(kept) or _unique(listed)


def already_have(
    finding: dict[str, Any], vendor: dict[str, Any] | None = None
) -> list[str]:
    reason = str(finding.get("reason") or "")
    items: list[str] = []
    if match := PAN_RE.search(reason):
        items.append(f"PAN number {match.group(1).upper()} written in the bid")
    if match := GSTIN_RE.search(reason):
        items.append(f"GSTIN {match.group(1).upper()} written in the bid")
    if match := LOCAL_PCT_RE.search(reason):
        items.append(f"{match.group(1)}% local content is declared in the bid")
    if match := IDENTITY_RE.search(reason):
        items.append(f"Legal identity for {match.group(1).strip()} is established")
    lowered = reason.casefold()
    if "land border" in lowered and any(
        word in lowered for word in ("provided", "submitted a signed")
    ):
        items.append("Land-border undertaking is already in the bid")
    if "test certificates" in lowered and "pages vendor-" in lowered:
        items.append("Bidder's own test certificates/letters are in the bid")
    identifiers = (_as_dict(vendor).get("identifiers") or {}) if vendor is not None else {}
    if isinstance(identifiers, dict):
        pan = str(identifiers.get("pan") or identifiers.get("PAN") or "").strip()
        gstin = str(identifiers.get("gstin") or identifiers.get("GSTIN") or "").strip()
        if pan:
            items.append(f"Vendor profile PAN {pan}")
        if gstin:
            items.append(f"Vendor profile GSTIN {gstin}")
    return _unique(items)[:6]


def classify_family(finding: dict[str, Any]) -> str:
    cid = _cid(finding).casefold()
    hay = _hay(finding)
    papers = " ".join(missing_papers(finding)).casefold()
    have = " ".join(already_have(finding)).casefold()

    if cid.endswith("-gstin") or "copy of gstin" in papers:
        return "gst"
    if cid.endswith("-udyam") or "udyam" in papers:
        return "udyam_mse"
    if cid.endswith("statutory_registrations") or cid.endswith("-pan") or "copy of pan card" in papers:
        return "pan"
    if "mse_startup" in cid or "mse benefits" in hay:
        if "benefits applicability" in hay or "udyam" in papers:
            return "udyam_mse"
        if "startup relaxation" in hay and re.search(r"relaxation[^.]{0,40}:\s*no", hay):
            return "startup_not_offered"
        if "relaxation for years of experience" in hay and re.search(r":\s*no", hay):
            return "mse_relaxation_not_offered"
        return "udyam_mse"
    if "maximum-allocation" in cid or "maximum allocation quantity" in hay:
        return "mii_allocation"
    if "similar_project" in cid or "pre-qualification" in hay or "annexure-pqr" in hay:
        return "pqr"
    if "non-disclosure" in cid or "non-disclosure agreement" in papers:
        return "nda"
    if "local-content" in cid or "make_in_india" in cid:
        return "mii"
    if cid.endswith("contractual_declarations"):
        land_present = "land border" in have
        wants_mii = "local content" in papers or "make in india" in papers or "local content" in hay
        wants_land = "land border" in papers or ("land border" in hay and not land_present)
        if wants_mii:
            return "mii"
        if wants_land:
            return "land_border"
        return "mii"
    if "permitted_participant" in cid:
        return "participation"
    if "quality-requirement" in cid or "correlated test" in hay or "original manufacturer" in papers:
        return "oem_tc"
    if any(marker in hay for marker in ("emd", "earnest money", "bid security")):
        return "emd"
    if "deviation" in hay or "loading criteria" in hay:
        actual_deviation = any(
            marker in hay
            for marker in (
                "deviation taken",
                "declared deviation",
                "does not accept",
                "exception to",
                "departs from",
                "deviates from",
            )
        )
        missing_statement = any(
            marker in hay
            for marker in (
                "deviation statement",
                "no-deviation statement",
                "no deviation statement",
                "deviation declaration",
            )
        ) and any(marker in hay for marker in ("missing", "not found", "not submitted", "not located"))
        return "deviation" if actual_deviation or not missing_statement else "deviation_statement"
    if "make in india" in hay or "local content" in hay:
        return "mii"
    if "udyam" in hay or "msme" in hay:
        return "udyam_mse"
    if "land border" in hay:
        return "land_border"
    return "document"


def _mii_is_eligibility(finding: dict[str, Any]) -> bool:
    hay = _hay(finding)
    return bool(
        re.search(r"only class[\s-]*[12].{0,90}eligible", hay)
        or re.search(r"non-local suppliers are not eligible", hay)
        or re.search(r"class-i and class-ii local suppliers are eligible to bid", hay)
    )


def _deviation_kind(finding: dict[str, Any]) -> str:
    category = str(finding.get("category") or "").casefold()
    if category in {"technical", "quality"}:
        return "technical"
    if category in {"commercial", "contractual"}:
        return "commercial"
    hay = _hay(finding)
    if any(marker in hay for marker in ("payment", "delivery term", "freight", "liquidated", "bank charge")):
        return "commercial"
    return "technical"


def _evidence_state(finding: dict[str, Any]) -> str:
    status = str(finding.get("status") or "").casefold()
    if status == "missing":
        return "missing"
    if status in {"unclear", "review", "needs_review", "not_evaluated"}:
        return "unclear"
    if finding.get("evidence") or finding.get("matched_documents"):
        return "deficient"
    return "missing"


def _tender_effect(finding: dict[str, Any], family: str) -> dict[str, str]:
    effect = str(finding.get("decision_effect") or finding.get("failure_effect") or "").casefold()
    mandatory = bool(finding.get("mandatory"))
    if family in {"startup_not_offered", "mse_relaxation_not_offered", "mii_allocation"}:
        return {
            "code": "no_bidder_effect",
            "label": "No bidder qualification impact",
            "reason": "This is a tender process rule, not evidence the bidder must submit.",
        }
    if effect in {"affects_price_ranking", "no_direct_fail"}:
        return {
            "code": "deny_preference",
            "label": "Benefit or preference is not established",
            "reason": "Continue the technical evaluation without granting the unproved benefit.",
        }
    if effect == "reject_or_preference_denied_based_on_tender":
        return {
            "code": "tender_dependent",
            "label": "Apply the consequence stated in the tender",
            "reason": "The tender must decide whether this is eligibility or only a preference condition.",
        }
    if effect == "block_financial_opening":
        return {
            "code": "block_financial_opening",
            "label": "Do not open the price bid while this remains unresolved",
            "reason": "The frozen tender checklist marks this as a financial-opening gate.",
        }
    if effect in {"reject_unresponsive", "reject_technical"} or mandatory:
        return {
            "code": "qualification_blocked",
            "label": "The bidder cannot pass this check while it remains unresolved",
            "reason": "The frozen tender checklist marks this requirement as mandatory.",
        }
    return {
        "code": "manual_review",
        "label": "Manual evaluation is required",
        "reason": "The frozen tender checklist does not define an automatic rejection consequence.",
    }


__all__ = [
    "_PASS",
    "_POST_AWARD_PHRASES",
    "PAN_RE",
    "GSTIN_RE",
    "LOCAL_PCT_RE",
    "IDENTITY_RE",
    "_is_post_award_finding",
    "_as_dict",
    "_hay",
    "_cid",
    "_unique",
    "_reason_says_present",
    "missing_papers",
    "already_have",
    "classify_family",
    "_mii_is_eligibility",
    "_deviation_kind",
    "_evidence_state",
    "_tender_effect",
]
