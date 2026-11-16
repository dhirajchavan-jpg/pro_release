"""Officer-facing finding copy. The wording is written by the live LLM."""

from __future__ import annotations

import logging
from typing import Any

from procurement.platform import llm as llm_runtime

_RELATED_APPROVALS = {
    ("bhel_purchase_policy", "5A.1"): ("5A.3",),
    ("bhel_purchase_policy", "5B.1"): ("5B.3",),
    ("bhel_works_policy", "5.1.1"): ("5.1.3",),
    ("bhel_works_policy", "5.2.1"): ("5.2.3",),
}

_logger = logging.getLogger(__name__)

_OFFICER_SYSTEM = (
    "You write short instructions for a BHEL purchase officer. "
    "The reader is not a lawyer and not a software person. "
    "Return only JSON."
)


def related_approval_clauses(policy_id: str, clause_id: str) -> tuple[str, ...]:
    return _RELATED_APPROVALS.get((policy_id, clause_id), ())


def _approval_rows(finding: dict[str, Any]) -> list[dict[str, str]]:
    clause = str(finding.get("clause_id") or "")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for approval in finding.get("approvals") or []:
        if not isinstance(approval, dict):
            continue
        title = str(approval.get("authority_title") or "").strip()
        action = str(approval.get("action") or "").strip()
        if not title:
            continue
        lowered = action.lower()
        if clause in {"5A.1", "5A.3"} and "release of emd" in lowered:
            continue
        if clause in {"22.1", "24.1"} and "waiver of penalty" in lowered:
            continue
        key = f"{title}|{action}"
        if key in seen:
            continue
        seen.add(key)
        rows.append({"who": title, "may_do": action})
    return rows


def _card_payload(finding: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        str(item.get("quote") or "").strip()
        for item in (finding.get("tender_evidence") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    return {
        "finding_id": finding.get("finding_id"),
        "clause_id": finding.get("clause_id"),
        "status": finding.get("status"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
        "policy_name": ((finding.get("policy_reference") or {}) if isinstance(finding.get("policy_reference"), dict) else {}).get("policy_name"),
        "clause_quote": str(finding.get("clause_quote") or "")[:700],
        "tender_quotes": evidence[:4],
        "approvals": _approval_rows(finding),
    }


def _clean_lines(value: Any, *, limit: int = 4) -> list[str]:
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


def apply_officer_language(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill officer_problem / officer_actions from one live LLM call. No stock phrases."""
    pending = [item for item in findings if isinstance(item, dict)
               and item.get("officer_copy_source") != "verified_package_review"]
    if not pending:
        return findings
    try:
        result = llm_runtime.llm_json(
            _OFFICER_SYSTEM,
            {
                "task": "officer_finding_copy",
                "section": "officer_copy",
                "instructions": (
                    "For each finding, write fresh sentences from the facts in this payload only. "
                    "problem: 2 to 4 complete, self-contained sentences. Say what the tender does, "
                    "and why that is a problem, using the amounts, dates, and quotes given. "
                    "actions: 2 to 4 complete, self-contained sentences. Tell the officer what to do "
                    "now. If approvals are listed, name that person in plain words and say what they "
                    "must approve. "
                    "Each bullet must make sense if read alone. "
                    "Do not paste raw policy titles joined by dashes. "
                    "Do not invent amounts, dates, or officers that are not in the payload. "
                    "Do not reuse a fixed script. Write for this tender only."
                ),
                "findings": [_card_payload(item) for item in pending],
                "json_shape": {
                    "cards": [
                        {
                            "finding_id": "same finding_id",
                            "problem": ["complete sentence"],
                            "actions": ["complete sentence"],
                        }
                    ]
                },
            },
            max_output_tokens=max(700, 220 * len(pending)),
        )
    except Exception:
        _logger.exception("officer copy LLM failed")
        for finding in pending:
            finding["officer_copy_source"] = "llm_failed"
        return findings
    cards = result.get("cards") if isinstance(result, dict) else None
    cards = cards if isinstance(cards, list) else []
    by_id = {
        str(card.get("finding_id") or ""): card
        for card in cards
        if isinstance(card, dict) and card.get("finding_id")
    }
    for finding in pending:
        card = by_id.get(str(finding.get("finding_id") or ""))
        if not card:
            finding["officer_copy_source"] = "llm_missing"
            continue
        problem = _clean_lines(card.get("problem"))
        actions = _clean_lines(card.get("actions"))
        if not problem and not actions:
            finding["officer_copy_source"] = "llm_empty"
            continue
        finding["officer_problem"] = problem
        finding["officer_actions"] = actions
        if actions:
            finding["required_action"] = " ".join(actions)
        finding["officer_copy_source"] = "llm"
    return findings
