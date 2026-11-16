"""Optional language enrichment for deterministic next-action cards."""

from __future__ import annotations

import logging
from typing import Any

from procurement.compliance.corpus import normalize_ws
from procurement.platform import llm as llm_runtime


logger = logging.getLogger("procurement.evaluation.next_ask")

_OFFICER_SYSTEM = (
    "You write short instructions for a BHEL purchase officer evaluating a bid. "
    "The reader is not a lawyer and not a software person. "
    "Return only JSON."
)

_LANGUAGE_BATCH = 5
_LANGUAGE_INSTRUCTIONS = (
    "For each card, write fresh sentences from the facts only. Plain English. "
    "problem: 1 or 2 short sentences. Name the bidder, the missing paper, and what is already on file. "
    "actions: 1 or 2 short sentences. Tell the officer the one thing to do now. "
    "If the ask-once door is open, the action is to ask for that paper. "
    "Do not mention Section Head, DGM, DoP, clause numbers, typed digits, digital substitutes, "
    "or recording a rejection. Those are not this screen. "
    "Do not name an approving officer unless approvals are listed and the door is a deviation with loading. "
    "Do not invent papers or officers. Do not reuse a script."
)


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


def _language_payload(card: dict[str, Any]) -> dict[str, Any]:
    doors = [door for door in (card.get("doors") or []) if isinstance(door, dict)]
    ask_open = any(door.get("id") == "ask_once" and door.get("open") for door in doors)
    if ask_open:
        doors = [door for door in doors if door.get("id") not in {"record_rejection", "no_path"}]
    approvals = []
    if not ask_open:
        for approval in card.get("approvals") or []:
            title = str(approval.get("authority_title") or "").strip()
            action = str(approval.get("action") or "").strip()
            if title:
                approvals.append({"who": title, "may_do": action})
    return {
        "criterion_id": card.get("criterion_id"),
        "vendor_name": card.get("vendor_name"),
        "family": card.get("family"),
        "title": card.get("title"),
        "reason": card.get("reason"),
        "missing_paper": card.get("missing_paper"),
        "already_have": card.get("already_have"),
        "primary_door": card.get("primary_door"),
        "facts_for_ask": card.get("facts_for_ask"),
        "doors": [
            {
                "id": door.get("id"),
                "open": door.get("open"),
                "label": door.get("label"),
            }
            for door in doors
        ],
        "approvals": approvals,
    }


def _fill_language_batch(batch: list[dict[str, Any]]) -> None:
    try:
        result = llm_runtime.llm_json(
            _OFFICER_SYSTEM,
            {
                "task": "bid_next_ask_copy",
                "section": "officer_copy",
                "instructions": _LANGUAGE_INSTRUCTIONS,
                "cards": [_language_payload(card) for card in batch],
                "json_shape": {
                    "cards": [
                        {
                            "criterion_id": "same criterion_id",
                            "problem": ["complete sentence"],
                            "actions": ["complete sentence"],
                        }
                    ]
                },
            },
            max_output_tokens=max(900, 280 * len(batch)),
        )
    except Exception:
        logger.exception("bid next-ask officer copy LLM failed")
        for card in batch:
            card["officer_copy_source"] = "llm_failed"
        return
    rows = result.get("cards") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    by_id = {
        str(row.get("criterion_id") or ""): row
        for row in rows
        if isinstance(row, dict) and row.get("criterion_id")
    }
    for card in batch:
        row = by_id.get(str(card.get("criterion_id") or ""))
        if not row:
            card["officer_copy_source"] = "llm_missing"
            continue
        problem = _clean_lines(row.get("problem"), limit=2)
        actions = _clean_lines(row.get("actions"), limit=2)
        if not problem and not actions:
            card["officer_copy_source"] = "llm_empty"
            continue
        card["officer_problem"] = problem
        card["officer_actions"] = actions
        card["officer_copy_source"] = "llm"


def apply_next_ask_language(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose evidence and actions without manufacturing stock narrative."""
    for card in cards:
        if not isinstance(card, dict):
            continue
        action = card.get("next_action") if isinstance(card.get("next_action"), dict) else {}
        reason = normalize_ws(card.get("reason") or "")
        action_label = normalize_ws(action.get("label") or "")
        card["officer_problem"] = [reason] if reason else []
        card["officer_actions"] = [action_label] if action_label else []
        card["officer_copy_source"] = "finding_and_policy"
    return cards


__all__ = [
    "logger",
    "_OFFICER_SYSTEM",
    "_LANGUAGE_BATCH",
    "_LANGUAGE_INSTRUCTIONS",
    "_clean_lines",
    "_language_payload",
    "_fill_language_batch",
    "apply_next_ask_language",
]
