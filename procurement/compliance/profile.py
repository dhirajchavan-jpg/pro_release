"""Tender mechanism profile and governing-policy selection.

The auditor first asks what this tender actually is (book, route, RA, FX,
split, EMD on/off). Unused policy machinery must never become a finding.
"""

from __future__ import annotations

import re
from typing import Any

from procurement.compliance.rules import field_map, value_text

PURCHASE_POLICY = "bhel_purchase_policy"
WORKS_POLICY = "bhel_works_policy"
_POLICIES = {PURCHASE_POLICY, WORKS_POLICY}
_PURCHASE_SPECIFIC_RE = re.compile(
    r"\b(?:goods?|materials?|equipment|spares?|plant|bop|turnkey|software|licen[cs]es?)\b",
    re.IGNORECASE,
)
_PURCHASE_GENERIC_RE = re.compile(r"\b(?:supply|purchase)\b", re.IGNORECASE)
_WORKS_RE = re.compile(
    r"\b(?:works?|services?|consulting|consultancy|construction|erection|civil|repair|maintenance|renovation|contract\s+labou?r)\b",
    re.IGNORECASE,
)
_PURCHASE_LED_MIXED_RE = re.compile(
    r"\b(?:bop|turnkey)\b|\b(?:equipment|plant)\b.{0,40}\brelated\s+services?\b",
    re.IGNORECASE,
)


def _haystack(understanding: dict[str, Any], *, source_chars: int = 12000) -> str:
    profile = understanding.get("tender_profile")
    profile = profile if isinstance(profile, dict) else {}
    parts = [
        str(profile.get(key) or "")
        for key in (
            "procurement_type",
            "scope",
            "scope_summary",
            "tender_title",
            "evaluation_method",
            "bid_system",
            "tender_number",
        )
    ]
    for field in field_map(understanding).values():
        parts.append(value_text(field.get("value")))
    units = understanding.get("source_units")
    if isinstance(units, dict):
        used = 0
        for unit_id in sorted(units):
            unit = units[unit_id]
            if not isinstance(unit, dict):
                continue
            text = str(unit.get("text") or "")
            if not text:
                continue
            take = text[:2000]
            parts.append(take)
            used += len(take)
            if used >= source_chars:
                break
    return " ".join(parts).lower()


def _hit(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def policy_scope(value: Any) -> str:
    """Classify policy scope without guessing across the two policy books."""
    text = " ".join(str(value or "").split())
    purchase_specific = bool(_PURCHASE_SPECIFIC_RE.search(text))
    purchase_generic = bool(_PURCHASE_GENERIC_RE.search(text))
    works = bool(_WORKS_RE.search(text))
    if works and purchase_specific:
        return "purchase" if _PURCHASE_LED_MIXED_RE.search(text) else "mixed"
    if works:
        return "works"
    if purchase_specific or purchase_generic:
        return "purchase"
    return "unknown"


def policy_id_for_profile(profile: dict[str, Any]) -> str:
    explicit = str(
        profile.get("governing_policy_id")
        or profile.get("policy_id")
        or ""
    ).strip()
    if explicit in _POLICIES:
        return explicit
    procurement_type = str(profile.get("procurement_type") or "").strip()
    scope = policy_scope(procurement_type)
    if scope == "unknown":
        scope = policy_scope(
            " ".join(
                str(profile.get(key) or "")
                for key in ("scope", "scope_summary", "tender_title")
            )
        )
    return {
        "purchase": PURCHASE_POLICY,
        "works": WORKS_POLICY,
    }.get(scope, "")


def bid_system_kind(profile: dict[str, Any]) -> str:
    text = " ".join(
        str(profile.get(key) or "")
        for key in ("bid_system", "evaluation_method")
    ).casefold()
    text = re.sub(r"[-_/]+", " ", text)
    patterns = {
        "single_part": r"\b(?:single|one|1)\s*(?:part|packet|bid|stage|cover|envelope)\b",
        "two_part": r"\b(?:two|2)\s*(?:part|packet|bid|stage|cover|envelope)\b",
        "three_part": r"\b(?:three|3)\s*(?:part|packet|bid|stage|cover|envelope)\b",
    }
    matches = {kind for kind, pattern in patterns.items() if re.search(pattern, text)}
    if not matches and "techno commercial" in text and "price" in text:
        matches.add("two_part")
    return next(iter(matches)) if len(matches) == 1 else "unknown"


def select_governing_policies(
    understanding: dict[str, Any],
    policy_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return the governing book, or audit both books when scope is unresolved."""
    if policy_id:
        detection = {
            "policy_id": policy_id,
            "confidence": 1.0,
            "reason": "policy selected by reviewer",
            "source": "manual",
        }
        return [policy_id], detection

    profile = understanding.get("tender_profile")
    profile = profile if isinstance(profile, dict) else {}
    selected = policy_id_for_profile(profile)
    if selected:
        return [selected], {
            "policy_id": selected,
            "reason": "governing policy selected from reviewed procurement type",
            "source": "tender_profile",
            "confidence": 1.0,
        }
    scope = policy_scope(profile.get("procurement_type"))
    return sorted(_POLICIES), {
        "policy_id": "",
        "confidence": 0.0,
        "reason": (
            "procurement scope spans both books; both policy audits were run"
            if scope == "mixed"
            else "procurement scope is unresolved; both policy audits were run"
        ),
        "source": "tender_profile",
        "status": "dual_policy_audit",
    }


def build_mechanism_profile(understanding: dict[str, Any]) -> dict[str, Any]:
    """Deterministic profile of what this tender actually uses. No LLM."""
    fields = field_map(understanding)
    profile = understanding.get("tender_profile")
    profile = profile if isinstance(profile, dict) else {}
    text = _haystack(understanding)
    tender_number = str(profile.get("tender_number") or "")
    identity = value_text((fields.get("tender_identity") or {}).get("value"))
    structure = value_text((fields.get("bid_structure_and_auction") or {}).get("value"))
    security = value_text(
        (fields.get("bid_security_and_fees") or fields.get("bid_security_fees") or {}).get("value")
    )
    combined = " ".join([text, tender_number, identity, structure, security])

    e_procurement = bool(
        _hit(combined, r"\be[- ]?proc", r"\be[- ]?tender", r"\bgem\b", r"gem portal", r"\bonline\b")
        or re.search(r"GEM/\d{4}/B/", combined, re.IGNORECASE)
    )
    if _hit(combined, r"\bsingle tender\b", r"\bsingle source\b", r"\bproprietary\b"):
        kind = "single"
    elif _hit(combined, r"\blimited tender\b"):
        kind = "limited"
    elif e_procurement:
        kind = "e-procurement"
    else:
        kind = "open"

    emd_text = security.lower()
    emd_waived = bool(re.search(r"\b(not required|exempt|nil|waived|not applicable)\b", emd_text))
    emd_required = None
    if emd_waived:
        emd_required = False
    elif re.search(r"\b(emd|bid security|earnest money)\b", emd_text) and re.search(
        r"\b(\d|rs|inr|₹|lakh|required)\b", emd_text
    ):
        emd_required = True

    ld_present = _hit(combined, r"\bliquidated damages\b", r"\bld\b", r"penalty for delay")
    deviation_loading = bool(
        re.search(
            r"\bdeviations?\b.{0,100}\b(?:permissible|permitted|allowed|accepted)\b.{0,140}\bloading\b"
            r"|\bloading criteria\b.{0,140}\bdeviations?\b",
            combined,
            re.IGNORECASE,
        )
        and not re.search(
            r"\bno(?:\s+[a-z]+){0,3}\s+deviations?\b"
            r"|\bdeviations?\b.{0,60}\b(?:not|never|no)\b.{0,30}\b(?:permissible|permitted|allowed|accepted)\b",
            combined,
            re.IGNORECASE,
        )
    )
    return {
        "tender_kind": kind,
        "e_procurement": e_procurement,
        "reverse_auction": _hit(combined, r"\breverse auction\b", r"\bra\b"),
        "two_part": bid_system_kind(profile) in {"two_part", "three_part"} or _hit(
            combined, r"two[- ]part", r"two[- ]packet", r"techno[- ]commercial", r"part[- ]i"
        ),
        "foreign_currency": _hit(
            combined, r"foreign currency", r"forex", r"usd", r"euro", r"exchange rate", r"sbi tt"
        ),
        "splitting": _hit(
            combined, r"\bsplit\b", r"more than one (supplier|source|contractor)", r"distribution of quantity"
        ),
        "email_offers": _hit(combined, r"e[- ]?mail offer", r"offers? by (fax|e-mail|email)"),
        "emd_required": emd_required,
        "emd_waived": emd_waived,
        "ps_required": (
            False
            if re.search(r"performance security.{0,40}(not required|nil|waived)", combined)
            else True
            if _hit(combined, r"performance security", r"performance bank guarantee", r"\bpbg\b")
            else None
        ),
        "ld_present": ld_present,
        "deviation_loading": deviation_loading,
        "customer_approved_sources": _hit(
            combined, r"customer approved", r"approved make", r"customer.?nominated"
        ),
        "price_variation": _hit(combined, r"price variation", r"\bpvc\b"),
        "framework_agreement": _hit(combined, r"framework agreement"),
        "mobilisation_advance": _hit(combined, r"mobilis[ae]tion advance"),
        "capital": _hit(combined, r"\bcapital (item|equipment|procurement)\b"),
        "eoi": _hit(combined, r"expression of interest", r"\beoi\b"),
        "consultant": _hit(combined, r"\bconsultant\b", r"\bconsultancy\b"),
        "evaluation_stated": _hit(
            combined, r"total cost to bhel", r"delivered cost", r"landed cost", r"evaluated on"
        ),
    }


def trigger_is_on(trigger: str, profile: dict[str, Any], haystack: str = "") -> bool:
    """True when this tender uses the mechanism the clause is conditioned on."""
    text = (trigger or "").lower()
    if not text:
        return False
    if "deviation" in text and "loading" in text:
        return bool(profile.get("deviation_loading"))
    checks = (
        ("reverse auction", profile.get("reverse_auction")),
        ("electronic", profile.get("e_procurement")),
        ("e-procurement", profile.get("e_procurement")),
        ("foreign currency", profile.get("foreign_currency")),
        ("forex", profile.get("foreign_currency")),
        ("split", profile.get("splitting")),
        ("email", profile.get("email_offers")),
        ("fax", profile.get("email_offers")),
        ("open tender", profile.get("tender_kind") in {"open", "e-procurement"}),
        ("performance security", profile.get("ps_required") is True),
        ("price variation", profile.get("price_variation")),
        ("fluctuation", profile.get("price_variation")),
        ("customer", profile.get("customer_approved_sources")),
        ("framework", profile.get("framework_agreement")),
        ("capital", profile.get("capital")),
        ("balance of plant", profile.get("capital")),
        ("mobilis", profile.get("mobilisation_advance")),
        ("expression of interest", profile.get("eoi")),
        ("eoi", profile.get("eoi")),
    )
    matched = False
    for needle, flag in checks:
        if needle in text:
            matched = True
            if flag:
                return True
    if matched:
        return False
    # Unknown condition: only activate when the tender itself uses the trigger words.
    tokens = [token for token in re.findall(r"[a-z]{4,}", text) if token not in {"shall", "must", "when", "only", "tender"}]
    return bool(tokens) and any(token in (haystack or "") for token in tokens[:4])
