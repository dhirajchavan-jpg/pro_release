"""Deterministic, no-LLM policy rule checks.

Every rule returns a finding dict or None. Rules never guess: when tender
facts are missing or unparseable the check is skipped (None).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from procurement.compliance.corpus import normalize_ws

# Clause ids differ between the two corpora; a rule is skipped for a policy
# when its clause is not listed here.
_RULE_CLAUSES = {
    "bhel_purchase_policy": {
        "notice_period": "8.2",
        "emd_slab": "5A.1",
        "emd_validity": "5A.2",
    },
    "bhel_works_policy": {
        "notice_period": "9.2",
        "emd_slab": "5.1.1",
    },
}

# (upper bound of estimated value in crore, required EMD in lakh).
_EMD_SLABS = {
    "bhel_purchase_policy": [(2.0, 0.0), (10.0, 6.0), (100.0, 40.0), (float("inf"), 300.0)],
    "bhel_works_policy": [(1.0, 0.0), (5.0, 2.0), (10.0, 5.0), (50.0, 20.0), (100.0, 30.0), (float("inf"), 50.0)],
}

# Bid-security return clause is located by corpus text, not hardcoded id.
_SECURITY_RETURN = {
    "bhel_purchase_policy": {"needles": ("30th day", "return"), "days": 30},
    "bhel_works_policy": {"needles": ("fifteen days", "refund"), "days": 15},
}

_DATE_VALUE_RE = r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}-\d{2}-\d{2})"
_EMD_EXEMPT_RE = re.compile(
    r"\b(not required|exempt(?:ed|ion)?|nil|waived|not applicable)\b",
    re.IGNORECASE,
)
_EMD_CONDITIONAL_EXEMPT_RE = re.compile(
    r"(?:mse|micro\s+and\s+small|startup|dpiit|psu|government|joint\s+venture|subsidiar|oem|consultancy|professional\s+service).{0,100}(?:exempt|waiv|not\s+required)"
    r"|(?:exempt|waiv|not\s+required).{0,100}(?:mse|micro\s+and\s+small|startup|dpiit|psu|government|joint\s+venture|subsidiar|oem|consultancy|professional\s+service)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# understanding access helpers
# ---------------------------------------------------------------------------

def source_units(understanding: dict[str, Any]) -> dict[str, dict[str, Any]]:
    units = understanding.get("source_units")
    if not isinstance(units, dict):
        return {}
    return {str(key): value for key, value in units.items()
            if isinstance(value, dict) and value.get("unit_type") != "anchor_fact"}


def field_map(understanding: dict[str, Any]) -> dict[str, dict[str, Any]]:
    container = understanding.get("field_understanding")
    sections = container.get("sections") if isinstance(container, dict) else None
    if not isinstance(sections, list):
        sections = understanding.get("sections") or []
    fields: dict[str, dict[str, Any]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        for field in section.get("fields") or []:
            if isinstance(field, dict) and field.get("key"):
                fields[str(field["key"])] = field
    return fields


def value_text(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}: {value_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "; ".join(value_text(item) for item in value)
    return str(value or "")


def named_value(value: Any, *labels: str) -> str:
    """First sub-value whose key mentions one of the labels (case-insensitive)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if any(label in str(key).replace("_", " ").casefold() for label in labels):
                return value_text(item)
    if isinstance(value, list):
        for item in value:
            key, separator, content = str(item).partition(":")
            if separator and any(label in key.casefold() for label in labels):
                return content.strip()
    return ""


def ground_evidence(understanding: dict[str, Any], evidence: Any) -> list[dict[str, Any]]:
    """Keep only evidence whose quote is a verbatim substring of a source unit."""
    units = source_units(understanding)
    items = evidence if isinstance(evidence, list) else [evidence] if isinstance(evidence, dict) else []
    grounded: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or item.get("text") or "").strip()
        if not quote:
            continue
        unit_id = _ground_quote(quote, units)
        if unit_id is None:
            continue
        grounded.append({"quote": quote, "page": units[unit_id].get("page"), "unit_id": unit_id})
    return grounded


def _ground_quote(quote: str, units: dict[str, dict[str, Any]]) -> str | None:
    for unit_id in sorted(units):
        if quote in str(units[unit_id].get("text") or ""):
            return unit_id
    normalized = normalize_ws(quote)
    if normalized:
        for unit_id in sorted(units):
            if normalized in normalize_ws(units[unit_id].get("text")):
                return unit_id
    return None


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def parse_date(text: Any) -> date | None:
    match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", str(text or ""))
    if match:
        first, second, year = (int(match.group(i)) for i in (1, 2, 3))
        for day, month in ((first, second), (second, first)):
            try:
                return date(year, month, day)
            except ValueError:
                continue
        return None
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", str(text or ""))
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _labeled_date(text: str, *labels: str) -> date | None:
    for label in labels:
        match = re.search(rf"{label}[^\d]{{0,60}}?{_DATE_VALUE_RE}", text, re.IGNORECASE)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def parse_amount_cr(text: Any) -> float | None:
    """Parse an Indian money expression into crore; None when not parseable."""
    value = str(text or "").lower()
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(crore|cr\.?|lakhs?|lacs?|l)\b", value)
    if match:
        number = float(match.group(1).replace(",", ""))
        unit = match.group(2)
        return number / 100.0 if unit.startswith("l") else number
    match = re.search(r"(?:rs\.?|₹|inr)\s*(\d[\d,]*(?:\.\d+)?)", value)
    if match:
        return float(match.group(1).replace(",", "")) / 10_000_000.0
    return None


# ---------------------------------------------------------------------------
# finding construction
# ---------------------------------------------------------------------------

def _clause(corpus: dict[str, Any], clause_id: str) -> dict[str, Any] | None:
    for clause in corpus.get("clauses") or []:
        if str(clause.get("clause_id")) == clause_id:
            return clause
    return None


def _find_clause_by_text(corpus: dict[str, Any], *needles: str) -> dict[str, Any] | None:
    for clause in corpus.get("clauses") or []:
        text = str(clause.get("text") or "").lower()
        if all(needle in text for needle in needles):
            return clause
    return None


def _clause_quote(clause: dict[str, Any]) -> str:
    text = normalize_ws(clause.get("text"))
    if len(text) > 700:
        cut = max(text.rfind(". ", 0, 700), text.rfind("; ", 0, 700))
        text = text[: cut + 1] if cut > 200 else text
    return text


def _finding(
    rule_slug: str,
    clause: dict[str, Any],
    severity: str,
    status: str,
    title: str,
    detail: str,
    evidence: list[dict[str, Any]],
    *,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clause_id = str(clause.get("clause_id") or "")
    payload = {
        "finding_id": f"rule-{rule_slug}",
        "clause_id": clause_id,
        "clause_title": str(clause.get("title") or clause.get("section_title") or ""),
        "clause_quote": _clause_quote(clause),
        "severity": severity,
        "status": status,
        "title": title,
        "detail": detail,
        "tender_evidence": evidence,
        "origin": "deterministic_rule",
        "resolution": {"status": "open", "note": "", "resolved_by": "", "resolved_at": ""},
    }
    if corpus:
        payload["policy_reference"] = {
            "policy_id": corpus.get("policy_id"),
            "policy_name": corpus.get("name"),
            "clause_id": clause_id,
        }
        payload["required_action"] = (
            "Correct the tender or record an authorized exception naming the approving authority, then re-run the audit."
            if status == "violation"
            else "Confirm the facts, then resolve or record an authorized exception naming the approving authority."
        )
        payload["consequence"] = (
            "The evaluation checklist stays locked until this is corrected or an authorized exception is recorded."
        )
    return payload


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def _unit_date_evidence(
    understanding: dict[str, Any], *labels: str
) -> tuple[date, dict[str, Any]] | tuple[None, None]:
    """Find a labeled date directly in the tender source units (grounded)."""
    units = source_units(understanding)
    for unit_id in sorted(units):
        text = str(units[unit_id].get("text") or "")
        found = _labeled_date(text, *labels)
        if found:
            match = re.search(
                rf"{labels[0]}[^\n]{{0,80}}", text, re.IGNORECASE
            )
            quote = (match.group(0) if match else text)[:160].strip()
            return found, {"quote": quote, "page": units[unit_id].get("page"), "unit_id": unit_id}
    return None, None


def check_notice_period(understanding: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any] | None:
    clause_id = _RULE_CLAUSES.get(corpus.get("policy_id"), {}).get("notice_period")
    clause = _clause(corpus, clause_id) if clause_id else None
    if clause is None:
        return None
    fields = field_map(understanding)
    timeline = fields.get("tender_timeline") or {}
    text = value_text(timeline.get("value"))
    published = _labeled_date(
        text, "publication", "published", "date of issue", "issue date", "bid dated", "bid start", "start date"
    )
    deadline = _labeled_date(
        text,
        "bid end",
        "submission deadline",
        "submission end",
        "submission close",
        "closing date",
        "due date",
        "end date",
        "last date",
        "submission",
    )
    evidence: list[dict[str, Any]] = []
    if published is None:
        published, unit_evidence = _unit_date_evidence(
            understanding, "bid dated", "publication date", "date of publication", "date of issue", "dated"
        )
        if unit_evidence:
            evidence.append(unit_evidence)
    if deadline is None:
        deadline, unit_evidence = _unit_date_evidence(
            understanding, "bid end date", "submission end date", "closing date", "due date", "last date of submission"
        )
        if unit_evidence:
            evidence.append(unit_evidence)
    missing = []
    if published is None:
        missing.append("the bid publication date")
    if deadline is None:
        missing.append("the bid submission deadline")
    if missing:
        return _finding(
            "notice-period",
            clause,
            "warning",
            "needs_review",
            "Notice period could not be confirmed",
            f"The tender does not state {' and '.join(missing)} clearly, so the notice "
            f"period required by Clause {clause['clause_id']} (minimum 3 weeks for open "
            "tenders, 2 weeks for limited tenders, 10 days for e-procurement) cannot be "
            "confirmed. Verify the dates manually.",
            evidence,
            corpus=corpus,
        )
    if not evidence:
        evidence = ground_evidence(understanding, timeline.get("evidence"))
    kind = _tender_kind(understanding, fields)
    required_days = {"open": 21, "limited": 14, "e-procurement": 10}[kind]
    actual_days = (deadline - published).days
    extension_note = ""
    corrigendum_date = _labeled_date(text, r"corrigendum[^.]{0,60}?extend", r"extend[^.]{0,40}?submission")
    if corrigendum_date and corrigendum_date > deadline:
        extension_note = (
            f" A corrigendum later extended the deadline to {corrigendum_date.isoformat()},"
            " but Clause "
            + clause["clause_id"]
            + " measures the original notice given."
        )
    if actual_days >= required_days:
        return None
    detail = (
        f"The tender was published on {published.isoformat()} with bid submission closing "
        f"on {deadline.isoformat()} — only {actual_days} days of notice. Clause "
        f"{clause['clause_id']} requires at least {required_days} days for {kind} tenders."
        + extension_note
    )
    return _finding(
        "notice-period",
        clause,
        "violation",
        "violation" if evidence else "needs_review",
        f"Notice period too short: {actual_days} days given, {required_days} required",
        detail if evidence else detail + " The tender quote could not be matched verbatim; confirm manually.",
        evidence,
        corpus=corpus,
    )


def _tender_kind(understanding: dict[str, Any], fields: dict[str, dict[str, Any]]) -> str:
    profile = understanding.get("tender_profile")
    if not isinstance(profile, dict):
        profile = {}
    structure = fields.get("bid_structure_and_auction") or {}
    signing = fields.get("digital_signing") or {}
    identity = fields.get("tender_identity") or {}
    text = " ".join(
        [
            str(profile.get("bid_system") or ""),
            str(profile.get("procurement_type") or ""),
            str(profile.get("tender_number") or ""),
            value_text(structure.get("value")),
            value_text(signing.get("value")),
            value_text(identity.get("value")),
        ]
    ).lower()
    if re.search(r"\b(e[- ]?proc|e[- ]?tender|online|gem)\b", text) or "gem portal" in text:
        return "e-procurement"
    if "limited" in text:
        return "limited"
    return "open"


def check_emd_slab(understanding: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any] | None:
    policy_id = str(corpus.get("policy_id") or "")
    clause_id = _RULE_CLAUSES.get(policy_id, {}).get("emd_slab")
    clause = _clause(corpus, clause_id) if clause_id else None
    if clause is None:
        return None
    fields = field_map(understanding)
    estimated_field = fields.get("estimated_procurement_value") or {}
    estimated_value = estimated_field.get("value")
    estimated_text = named_value(estimated_value, "estimated value", "estimated cost", "estimated", "amount") or value_text(estimated_value)
    estimated_cr = parse_amount_cr(estimated_text)
    if estimated_cr is None:
        security_field0 = fields.get("bid_security_and_fees") or fields.get("bid_security_fees") or {}
        security_text0 = value_text(security_field0.get("value")).lower()
        if _EMD_EXEMPT_RE.search(security_text0) and not _EMD_CONDITIONAL_EXEMPT_RE.search(security_text0):
            return None
        return _finding(
            "emd-slab",
            clause,
            "warning",
            "needs_review",
            "EMD requirement could not be confirmed",
            "The tender does not state its estimated value clearly, so the EMD required by "
            f"Clause {clause['clause_id']} (slab by estimated value) cannot be confirmed. "
            "Verify the EMD amount manually.",
            [],
            corpus=corpus,
        )
    required_lakh = next(lakh for bound, lakh in _EMD_SLABS[policy_id] if estimated_cr <= bound)
    security_field = fields.get("bid_security_and_fees") or fields.get("bid_security_fees") or {}
    security_value = security_field.get("value")
    emd_text = named_value(security_value, "emd", "bid security", "earnest money") or value_text(security_value)
    evidence = ground_evidence(
        understanding,
        list(estimated_field.get("evidence") or []) + list(security_field.get("evidence") or []),
    )
    if required_lakh <= 0:
        return None
    emd_lakh: float | None = None
    if emd_text.strip():
        parsed = parse_amount_cr(emd_text)
        emd_lakh = parsed * 100.0 if parsed is not None else None
        exemption = _EMD_EXEMPT_RE.search(emd_text)
        conditional_exemption = _EMD_CONDITIONAL_EXEMPT_RE.search(emd_text)
        if exemption and emd_lakh is None and conditional_exemption:
            return _finding(
                "emd-slab",
                clause,
                "warning",
                "needs_review",
                "EMD amount for non-exempt bidders is not clear",
                "The tender mentions a bidder-category exemption but does not clearly state the EMD amount applicable to other bidders.",
                evidence,
                corpus=corpus,
            )
        if exemption and emd_lakh is None:
            waiver = _clause(corpus, "5A.3") if policy_id == "bhel_purchase_policy" else _clause(corpus, "5.1.3")
            waiver = waiver or clause
            approval = (
                "Purchase Policy 5A.3 requires Head of Contracting (AGM+) approval for its listed cases and Unit Head approval for other cases."
                if policy_id == "bhel_purchase_policy"
                else "Works Policy 5.1.3 requires Head of Contracting (AGM+) approval for its listed cases and ED/Unit Head approval for other cases."
            )
            return _finding(
                "emd-waiver",
                waiver,
                "warning",
                "needs_review",
                "EMD is waived — record approval from the competent authority",
                (
                    f"At an estimated value of about Rs {estimated_cr:g} crore, Clause "
                    f"{clause['clause_id']} requires EMD of Rs {required_lakh:g} lakh. "
                    f"The tender states EMD is not required. Do not float this waiver without approval. {approval}"
                ),
                evidence,
                corpus=corpus,
            )
    if emd_lakh is None:
        detail = (
            f"With an estimated value of about Rs {estimated_cr:g} crore, Clause "
            f"{clause['clause_id']} requires EMD of Rs {required_lakh:g} lakh, but the tender's "
            "EMD amount could not be read clearly. Verify it manually."
        )
        return _finding(
            "emd-slab",
            clause,
            "warning",
            "needs_review",
            "EMD required by policy slab — tender EMD unclear",
            detail,
            evidence,
            corpus=corpus,
        )
    if emd_lakh >= required_lakh:
        return None
    detail = (
        f"With an estimated value of about Rs {estimated_cr:g} crore, Clause {clause['clause_id']} "
        f"requires EMD of Rs {required_lakh:g} lakh, but the tender specifies only Rs {emd_lakh:g} lakh."
    )
    return _finding(
        "emd-slab",
        clause,
        "violation",
        "violation" if evidence else "needs_review",
        f"EMD too low: Rs {emd_lakh:g} lakh specified, Rs {required_lakh:g} lakh required",
        detail if evidence else detail + " The tender quote could not be matched verbatim; confirm manually.",
        evidence,
        corpus=corpus,
    )


def check_emd_validity(understanding: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any] | None:
    clause_id = _RULE_CLAUSES.get(corpus.get("policy_id"), {}).get("emd_validity")
    clause = _clause(corpus, clause_id) if clause_id else None
    if clause is None:
        return None
    fields = field_map(understanding)
    security_field = fields.get("bid_security_and_fees") or fields.get("bid_security_fees") or {}
    text = value_text(security_field.get("value"))
    if re.search(r"\b(not required|exempt|nil|waived|not applicable)\b", text.lower()):
        return None
    emd_days = _days_near(text, r"emd|bid security|earnest money", r"valid")
    bid_days = _days_near(text, r"bid|offer", r"validity")
    if emd_days is None or bid_days is None:
        return None
    required = bid_days + 45
    if emd_days >= required:
        return None
    evidence = ground_evidence(understanding, security_field.get("evidence"))
    detail = (
        f"The tender keeps EMD valid for {emd_days} days, shorter than the final bid validity "
        f"of {bid_days} days plus the 45-day margin required by Clause {clause['clause_id']}."
    )
    return _finding(
        "emd-validity",
        clause,
        "violation",
        "violation" if evidence else "needs_review",
        "EMD validity too short (bid validity + 45 days required)",
        detail if evidence else detail + " The tender quote could not be matched verbatim; confirm manually.",
        evidence,
        corpus=corpus,
    )


def _days_near(text: str, subject: str, attribute: str) -> int | None:
    patterns = (
        rf"(?:{subject})[^.]{{0,120}}?(?:{attribute})[^.]{{0,80}}?(\d+)\s*(?:\([^)]*\)\s*)?days",
        rf"(\d+)\s*(?:\([^)]*\)\s*)?days[^.]{{0,80}}?(?:{attribute})[^.]{{0,80}}?(?:{subject})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def check_security_return(understanding: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any] | None:
    config = _SECURITY_RETURN.get(str(corpus.get("policy_id") or ""))
    if not config:
        return None
    clause = _find_clause_by_text(corpus, *config["needles"])
    if clause is None:
        return None
    fields = field_map(understanding)
    security_field = fields.get("bid_security_and_fees") or fields.get("bid_security_fees") or {}
    text = value_text(security_field.get("value"))
    if re.search(r"\b(not required|exempt|nil|waived|not applicable)\b", text.lower()):
        return None
    days = None
    for pattern in (
        r"(?:return|refund)[^.]{0,120}?within\s*(?:about\s*)?(\d+)\s*days",
        r"within\s*(?:about\s*)?(\d+)\s*days[^.]{0,120}?(?:return|refund)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            days = int(match.group(1))
            break
    if days is None or days <= config["days"]:
        return None
    evidence = ground_evidence(understanding, security_field.get("evidence"))
    detail = (
        f"The tender allows {days} days to return or refund bid security; Clause "
        f"{clause['clause_id']} requires return within {config['days']} days."
    )
    return _finding(
        "security-return",
        clause,
        "violation",
        "violation" if evidence else "needs_review",
        f"Bid security return period too long ({days} days, max {config['days']})",
        detail if evidence else detail + " The tender quote could not be matched verbatim; confirm manually.",
        evidence,
        corpus=corpus,
    )


def hosted_document_clause(corpus: dict[str, Any]) -> dict[str, Any] | None:
    for clause in corpus.get("clauses") or []:
        text = normalize_ws(clause.get("text")).casefold()
        if "tender documents shall be hosted" in text and "modalities for sending" in text:
            return clause
    return None


def check_ld_clause(understanding: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any] | None:
    """LD must appear in the NIT. Works: leave it out only with Unit Head."""
    policy_id = str(corpus.get("policy_id") or "")
    clause = _clause(corpus, "24.1" if policy_id == "bhel_works_policy" else "22.1")
    if clause is None:
        return None
    haystack = " ".join(
        [
            value_text(field.get("value"))
            for field in field_map(understanding).values()
        ]
        + [
            str((unit or {}).get("text") or "")
            for unit in (understanding.get("source_units") or {}).values()
            if isinstance(unit, dict)
        ]
    )
    if re.search(r"\b(liquidated damages|\bld\b|penalty for delay)\b", haystack, re.IGNORECASE):
        return None
    if re.search(r"\bgem\b.{0,80}\b(gtc|atc|general terms)\b|\bgeneral terms and conditions\b", haystack, re.IGNORECASE):
        return None
    works = policy_id == "bhel_works_policy"
    return _finding(
        "ld-clause",
        clause,
        "warning",
        "needs_review",
        "No liquidated-damages clause in the tender",
        (
            "Works Policy 24.1 requires an LD/penalty clause in the NIT. Leaving it out "
            "needs Unit Head approval with recorded reasons."
            if works
            else "Purchase Policy 22.1 requires a proper LD/penalty clause in the NIT. "
            "Waiving LD later needs Finance concurrence and DoP approval."
        ),
        [],
        corpus=corpus,
    )


_RULE_CHECKS = (
    check_notice_period,
    check_emd_slab,
    check_emd_validity,
    check_security_return,
    check_ld_clause,
)


def run_rules(understanding: dict[str, Any], corpus: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for check in _RULE_CHECKS:
        finding = check(understanding, corpus)
        if finding:
            findings.append(finding)
    return findings
