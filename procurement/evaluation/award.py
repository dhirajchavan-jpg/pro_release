"""Project-level award suggestion after every bidder has been evaluated.

This does not change v1 qualify/reject and does not invent an L1 price.
It tells the officer: may we award, who is in the price-opening set, or
if nobody is responsive, who is worth asking once under clause 8.6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from procurement.evaluation.next_ask import cards_by_criterion
from procurement.platform import llm as llm_runtime

logger = logging.getLogger(__name__)

SCHEMA = "bid-award-recommendation.v1"

_PASS = {
    "pass",
    "passed",
    "qualified",
    "compliant",
    "met",
    "not_evaluated",
    "not_applicable",
}
_FAIL = {"fail", "failed", "missing", "missing_after_complete_search"}

_OFFICER_SYSTEM = (
    "You write short instructions for a BHEL purchase officer choosing among evaluated bids. "
    "The reader is not a lawyer and not a software person. "
    "Return only JSON."
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def source_hash(
    bidders: list[dict[str, Any]],
    tender: dict[str, Any] | None = None,
) -> str:
    payload = []
    for item in sorted(bidders, key=lambda row: str(row.get("vendor_key") or "")):
        payload.append(
            {
                "vendor_key": item.get("vendor_key"),
                "evaluation_id": item.get("evaluation_id"),
                "qualifies": item.get("qualifies"),
                "final_decision": item.get("final_decision"),
                "findings": [
                    {
                        "criterion_id": finding.get("criterion_id"),
                        "status": finding.get("status"),
                    }
                    for finding in item.get("findings") or []
                ],
            }
        )
    raw = json.dumps(
        {"bidders": payload, "tender": tender or {}},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hay(finding: dict[str, Any]) -> str:
    return " ".join(
        str(part or "")
        for part in (
            finding.get("criterion_id"),
            finding.get("criterion_text"),
            finding.get("reason"),
        )
    ).casefold()


def _is_pass(status: str) -> bool:
    return str(status or "").casefold() in _PASS


def _card_for(finding: dict[str, Any], cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return cards.get(str(finding.get("criterion_id") or "")) or {}


def _local_content_declared(finding: dict[str, Any], card: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            str(finding.get("reason") or ""),
            " ".join(card.get("already_have") or []),
        ]
    ).casefold()
    return "%" in blob and "local content" in blob


def summarize_bidder(item: dict[str, Any]) -> dict[str, Any]:
    findings = [_as_dict(finding) for finding in item.get("findings") or []]
    cards = cards_by_criterion(item.get("next_ask") or {})
    qualifies = bool(item.get("qualifies"))
    decision = str(item.get("final_decision") or "")
    recommendation = str(item.get("recommendation") or "")
    if qualifies:
        band = "qualified"
    elif recommendation == "needs_manual_review" or "review" in decision.casefold():
        band = "review"
    else:
        band = "unresponsive"

    hard_walls: list[str] = []
    ask_once: list[str] = []
    preference_notes: list[str] = []
    strengths: list[str] = []
    process_only = 0
    scored = 0
    declared_mii = any(
        _local_content_declared(finding, _card_for(finding, cards))
        for finding in findings
    )

    if band == "qualified":
        scored += 10000
    elif band == "review":
        scored += 5000
    if declared_mii:
        strengths.append("A local-content percentage is already declared in the bid.")
        scored += 400

    for finding in findings:
        status = str(finding.get("status") or "").casefold()
        card = _card_for(finding, cards)
        family = str(card.get("family") or "")
        tender_effect = card.get("tender_effect") if isinstance(card.get("tender_effect"), dict) else {}
        effect_code = str(tender_effect.get("code") or "")
        title = str(finding.get("criterion_text") or finding.get("criterion_id") or "")
        paper = str(card.get("missing_paper") or title)[:160]
        cid = str(finding.get("criterion_id") or "")

        if effect_code == "no_bidder_effect" or family in {
            "startup_not_offered",
            "mse_relaxation_not_offered",
            "mii_allocation",
        }:
            if not _is_pass(status):
                process_only += 1
            continue

        if _is_pass(status):
            if family == "pqr" or "similar_project" in cid:
                strengths.append("Pre-qualification evidence is on file.")
                scored += 800
            elif family == "mii" or "make_in_india" in cid:
                strengths.append("Make in India class/local-content evidence passed.")
                scored += 700
            elif family == "udyam_mse" or "udyam" in cid or "mse_startup" in cid:
                strengths.append("MSE/Udyam evidence is on file.")
                scored += 150
            elif "land" in cid or family == "land_border":
                strengths.append("Land-border undertaking is on file.")
                scored += 200
            continue

        if effect_code == "deny_preference":
            preference_notes.append(str(tender_effect.get("label") or "The unproved preference is not available."))
            continue

        if family == "udyam_mse" and str(card.get("primary_door") or "") == "preference_closed":
            preference_notes.append("MSE preference is closed until valid Udyam evidence is on file.")
            continue

        if effect_code in {"qualification_blocked", "block_financial_opening"}:
            hard_walls.append(paper or title[:120])
            scored -= 900
            continue

        eligibility = bool(card.get("eligibility_gate"))
        if family == "pqr" or "similar_project" in cid:
            hard_walls.append(paper or "Pre-qualification evidence")
            scored -= 4000
        elif family == "land_border" and not any("land-border" in str(x).casefold() for x in card.get("already_have") or []):
            hard_walls.append(paper or "Land-border / DPIIT registration")
            scored -= 3500
        elif family == "mii" and eligibility and not declared_mii:
            hard_walls.append(paper or "Make in India class certificate")
            scored -= 3000
        elif family == "mii":
            ask_once.append(paper or "Make in India certificate to be pinned to a verified page")
            scored -= 80
        elif family == "participation":
            ask_once.append(paper or "Participation / Class I-II proof")
            scored -= 120
        elif family in {"pan", "gst", "nda"}:
            ask_once.append(paper)
            scored -= 80
        elif family == "oem_tc":
            if status in {"unclear", "review"} or any("test certificate" in str(x).casefold() for x in card.get("already_have") or []):
                ask_once.append(paper or "Original-manufacturer test certificate")
                scored -= 60
            else:
                ask_once.append(paper or "Original-manufacturer test certificate")
                scored -= 150
        elif eligibility:
            hard_walls.append(paper or title[:120])
            scored -= 900
        else:
            ask_once.append(paper or title[:120])
            scored -= 100

    prices_open = bool((item.get("financial_result") or {}).get("opening_allowed"))
    evaluated_price = (item.get("financial_result") or {}).get("evaluated_price")

    return {
        "vendor_key": item.get("vendor_key"),
        "vendor_name": item.get("vendor_name") or item.get("vendor_key"),
        "band": band,
        "qualifies": qualifies,
        "final_decision": decision,
        "passed": sum(1 for finding in findings if _is_pass(str(finding.get("status") or ""))),
        "failed": sum(1 for finding in findings if str(finding.get("status") or "").casefold() in _FAIL),
        "review": sum(
            1
            for finding in findings
            if str(finding.get("status") or "").casefold() not in _PASS | _FAIL
        ),
        "hard_walls": _unique(hard_walls),
        "ask_once": _unique(ask_once),
        "preference_notes": _unique(preference_notes),
        "strengths": _unique(strengths),
        "score": scored,
        "prices_open": prices_open,
        "evaluated_price": evaluated_price,
        "can_pursue": band != "unresponsive" or (not hard_walls and ask_once),
        "mse_preference_open": not any("MSE preference is closed" in note for note in preference_notes)
        and any("MSE/Udyam" in note for note in strengths),
    }


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = " ".join(str(item or "").split()).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def assemble_award_recommendation(
    bidders: list[dict[str, Any]],
    *,
    tender: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tender = tender or {}
    snapshots = [summarize_bidder(item) for item in bidders if item.get("vendor_key")]
    qualified = [item for item in snapshots if item["band"] == "qualified"]
    review = [item for item in snapshots if item["band"] == "review"]
    unresponsive = [item for item in snapshots if item["band"] == "unresponsive"]
    any_price = any(item.get("prices_open") and item.get("evaluated_price") for item in snapshots)

    if qualified:
        ranked = sorted(qualified, key=lambda item: (-item["score"], str(item["vendor_name"])))
        preferred = ranked[0]
        if len(qualified) == 1:
            action = "open_price_for_qualified"
            preferred_role = "price_opening_set"
        elif any_price:
            action = "rank_qualified_on_opened_price"
            preferred_role = "price_opening_set"
        else:
            action = "open_price_for_qualified_set"
            preferred_role = "price_opening_set"
        awardable = True
    elif review:
        ranked = sorted(review, key=lambda item: (-item["score"], str(item["vendor_name"])))
        preferred = ranked[0]
        action = "human_review_before_price"
        preferred_role = "review_first"
        awardable = False
    else:
        pursue = [item for item in snapshots if not item["hard_walls"]]
        pool = pursue or snapshots
        ranked = sorted(pool, key=lambda item: (-item["score"], str(item["vendor_name"])))
        preferred = ranked[0] if ranked else None
        action = "do_not_award"
        preferred_role = "pursue_via_ask_once" if preferred and not (preferred or {}).get("hard_walls") else "no_awardable_bid"
        awardable = False

    mse_set = [item["vendor_key"] for item in qualified if item.get("mse_preference_open")]
    facts = {
        "tender_title": tender.get("tender_title") or tender.get("title") or "",
        "tender_number": tender.get("tender_number") or "",
        "bid_system": tender.get("bid_system") or "",
        "procurement_type": tender.get("procurement_type") or "",
        "prices_opened": any_price,
        "awardable_now": awardable,
        "action": action,
        "preferred_role": preferred_role,
        "preferred_vendor_key": (preferred or {}).get("vendor_key"),
        "preferred_vendor_name": (preferred or {}).get("vendor_name"),
        "qualified_vendor_keys": [item["vendor_key"] for item in qualified],
        "mse_preference_vendor_keys": mse_set,
        "mii_note": (
            "This tender restricts participation to Class I and Class II local suppliers. "
            "MSE preference is only for manufacturer/OEM Udyam, not traders. "
            "Price ranking is not available until a technically qualified set exists and price bids are opened."
        ),
        "bidders": snapshots,
        "facts_for_copy": _facts_for_copy(snapshots, preferred, action, awardable, any_price),
    }
    return {
        "schema_version": SCHEMA,
        "source_hash": source_hash(bidders, tender),
        "action": action,
        "awardable_now": awardable,
        "preferred_vendor_key": facts["preferred_vendor_key"],
        "preferred_vendor_name": facts["preferred_vendor_name"],
        "preferred_role": preferred_role,
        "qualified_vendor_keys": facts["qualified_vendor_keys"],
        "mse_preference_vendor_keys": mse_set,
        "prices_opened": any_price,
        "bidders": snapshots,
        "facts": facts,
        "officer_headline": "",
        "officer_why": [],
        "officer_others": [],
        "officer_actions": [],
        "officer_copy_source": "pending",
    }


def _facts_for_copy(
    snapshots: list[dict[str, Any]],
    preferred: dict[str, Any] | None,
    action: str,
    awardable: bool,
    any_price: bool,
) -> list[str]:
    lines: list[str] = []
    if not awardable:
        lines.append("Do not award and do not open price bids. Nobody is technically responsive yet.")
    if action == "do_not_award" and preferred and not preferred.get("hard_walls"):
        lines.append(
            f"If the officer uses the two-part ask-once path, start with {preferred.get('vendor_name')} "
            "because their remaining gaps are papers that can be furnished, not a missing qualification."
        )
    if action == "do_not_award" and preferred and preferred.get("hard_walls"):
        lines.append(
            f"Even the closest bid ({preferred.get('vendor_name')}) still has a qualification wall. "
            "There is no bidder to prefer for award."
        )
    if awardable and not any_price:
        names = ", ".join(item.get("vendor_name") or "" for item in snapshots if item.get("qualifies"))
        lines.append(
            f"Technically qualified set: {names}. Open their price bids. Do not name an L1 until prices are opened."
        )
    if not any_price:
        lines.append("No evaluated price is available. Do not invent an L1 or an award value.")
    for item in snapshots:
        name = item.get("vendor_name")
        if item.get("hard_walls"):
            lines.append(f"{name} hard walls: " + "; ".join(item["hard_walls"]))
        if item.get("ask_once"):
            lines.append(f"{name} ask-once papers: " + "; ".join(item["ask_once"][:6]))
        if item.get("strengths"):
            lines.append(f"{name} already has: " + "; ".join(item["strengths"][:4]))
        if item.get("preference_notes"):
            lines.append(f"{name}: " + "; ".join(item["preference_notes"]))
    return lines


def _clean_lines(value: Any, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split()).strip()
        if not text:
            continue
        if text[-1] not in ".?!":
            text += "."
        if text not in lines:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def _apply_deterministic_award_language(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose the computed recommendation even when prose generation is unavailable."""
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    lines = _clean_lines(facts.get("facts_for_copy"), limit=16)
    preferred_name = str(payload.get("preferred_vendor_name") or "").strip()
    bidders = [item for item in payload.get("bidders") or [] if isinstance(item, dict)]

    headline = lines[0] if lines else "No award recommendation is available from the completed evaluations."
    preferred_lines = [line for line in lines[1:] if preferred_name and preferred_name.casefold() in line.casefold()]
    other_names = {
        str(item.get("vendor_name") or "").strip().casefold()
        for item in bidders
        if str(item.get("vendor_name") or "").strip()
        and str(item.get("vendor_name") or "").strip().casefold() != preferred_name.casefold()
    }
    other_lines = [
        line
        for line in lines[1:]
        if any(name in line.casefold() for name in other_names)
    ]
    action_lines = [
        line
        for line in lines[1:]
        if line not in preferred_lines and line not in other_lines
    ]

    payload["officer_headline"] = headline
    payload["officer_why"] = preferred_lines[:5]
    payload["officer_others"] = other_lines[:6]
    payload["officer_actions"] = action_lines[:4]
    payload["officer_copy_source"] = "deterministic"
    return payload


def apply_award_language(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _apply_deterministic_award_language(payload)
    if os.getenv("AI_AWARD_COPY_LLM_ENABLED", "0") != "1":
        return payload
    facts = payload.get("facts") or {}
    try:
        result = llm_runtime.llm_json(
            _OFFICER_SYSTEM,
            {
                "task": "bid_award_suggestion",
                "section": "officer_copy",
                "instructions": (
                    "Write fresh sentences from the facts only. "
                    "headline: 1 or 2 complete sentences, first-person officer tone like "
                    "'From what I have seen in these bids, ...'. "
                    "If nobody is awardable, say so in the headline and name who to pursue only as an ask-once path, not as an award. "
                    "If someone is qualified but prices are unopened, name them as the bid to take to price opening, not as L1. "
                    "why: 3 to 5 complete sentences about the preferred bid. "
                    "others: 1 or 2 complete sentences per other bidder saying why they are not preferred. "
                    "actions: 2 to 4 complete sentences telling the officer what to do now. "
                    "Do not invent a price, L1 rank, Udyam, Make in India class, or PAN card that is not in the facts. "
                    "Do not tell the officer to waive PQR, land-border, or Class I/II eligibility. "
                    "Do not paste policy titles joined by dashes."
                ),
                "facts": {
                    "tender_title": facts.get("tender_title"),
                    "tender_number": facts.get("tender_number"),
                    "action": payload.get("action"),
                    "awardable_now": payload.get("awardable_now"),
                    "preferred_vendor_name": payload.get("preferred_vendor_name"),
                    "preferred_role": payload.get("preferred_role"),
                    "prices_opened": payload.get("prices_opened"),
                    "mii_note": facts.get("mii_note"),
                    "lines": facts.get("facts_for_copy"),
                    "bidders": [
                        {
                            "name": item.get("vendor_name"),
                            "band": item.get("band"),
                            "hard_walls": item.get("hard_walls"),
                            "ask_once": item.get("ask_once"),
                            "strengths": item.get("strengths"),
                            "preference_notes": item.get("preference_notes"),
                        }
                        for item in payload.get("bidders") or []
                    ],
                },
                "json_shape": {
                    "headline": "complete sentence",
                    "why": ["complete sentence"],
                    "others": ["complete sentence"],
                    "actions": ["complete sentence"],
                },
            },
            max_output_tokens=1200,
        )
    except Exception:
        logger.exception("award suggestion officer copy LLM failed")
        return payload
    if not isinstance(result, dict):
        return payload
    headline = " ".join(str(result.get("headline") or "").split()).strip()
    why = _clean_lines(result.get("why"), limit=5)
    others = _clean_lines(result.get("others"), limit=6)
    actions = _clean_lines(result.get("actions"), limit=4)
    if not headline and not why:
        return payload
    if headline and headline[-1] not in ".?!":
        headline += "."
    payload["officer_headline"] = headline
    payload["officer_why"] = why
    payload["officer_others"] = others
    payload["officer_actions"] = actions
    payload["officer_copy_source"] = "llm"
    return payload


def apply_award_recommendation(
    bidders: list[dict[str, Any]],
    *,
    tender: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = assemble_award_recommendation(bidders, tender=tender)
    return apply_award_language(payload)


def public_award_view(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        return None
    return {
        "schema_version": SCHEMA,
        "action": payload.get("action"),
        "awardable_now": payload.get("awardable_now"),
        "preferred_vendor_key": payload.get("preferred_vendor_key"),
        "preferred_vendor_name": payload.get("preferred_vendor_name"),
        "preferred_role": payload.get("preferred_role"),
        "qualified_vendor_keys": payload.get("qualified_vendor_keys") or [],
        "mse_preference_vendor_keys": payload.get("mse_preference_vendor_keys") or [],
        "prices_opened": payload.get("prices_opened"),
        "officer_headline": payload.get("officer_headline") or "",
        "officer_why": payload.get("officer_why") or [],
        "officer_others": payload.get("officer_others") or [],
        "officer_actions": payload.get("officer_actions") or [],
        "officer_copy_source": payload.get("officer_copy_source"),
        "bidders": [
            {
                "vendor_key": item.get("vendor_key"),
                "vendor_name": item.get("vendor_name"),
                "band": item.get("band"),
                "passed": item.get("passed"),
                "failed": item.get("failed"),
                "hard_walls": item.get("hard_walls") or [],
                "ask_once": item.get("ask_once") or [],
                "strengths": item.get("strengths") or [],
            }
            for item in payload.get("bidders") or []
        ],
    }
