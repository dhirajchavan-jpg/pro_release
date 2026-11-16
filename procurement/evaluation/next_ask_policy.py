"""Policy-corpus lookup and grounded clause enrichment for next-action cards."""

from __future__ import annotations

import logging
import re
from typing import Any

from procurement.compliance.approvals import approvals_for
from procurement.compliance.corpus import lexical_rank, load_corpus, normalize_ws
from procurement.compliance.profile import bid_system_kind, policy_id_for_profile
from procurement.platform import llm as llm_runtime


PURCHASE = "bhel_purchase_policy"
WORKS = "bhel_works_policy"

logger = logging.getLogger("procurement.evaluation.next_ask")

_PROCESS = {
    PURCHASE: {"ask_once": "8.6", "single_part": "8.1", "deviation": "8.7"},
    WORKS: {"ask_once": "9.6", "single_part": "9.1", "deviation": "9.7"},
}

# Only families whose corpus clause is actually about that subject.
_SUBJECT = {
    PURCHASE: {"emd": "5A"},
    WORKS: {"emd": "5.1"},
}

# 5A excepts MSEs/Startups from EMD. Works 5.1 does not. Do not cite 5A.3 —
# that is the AGM PSU/JV waiver, not the MSE exception.
_EMD_MSE_CLAUSE = {PURCHASE: "5A"}

# Process / kitchen-sink clauses must not be offered as "the subject clause".
_PROCESS_CLAUSE_IDS = {"8.1", "8.6", "8.7", "9.1", "9.6", "9.7"}
_BLOCKED_CLAUSE_IDS = _PROCESS_CLAUSE_IDS | {
    "26.0",
    "32.0",
    "34.0",
    "34.1",
    "34.2",
    "34.3",
    "35.0",
    "35.1",
    "35.2",
    "35.3",
}
_FAMILY_QUERY = {
    "pan": "PAN permanent account number vendor code creation copy of PAN card",
    "gst": "GST GSTIN goods and services tax registration certificate vendor code",
    "udyam_mse": "MSE micro small enterprise Udyam manufacturer OEM trader purchase preference",
    "startup_not_offered": "startup relaxation experience turnover DPIIT",
    "mse_relaxation_not_offered": "MSE relaxation experience turnover",
    "emd": "earnest money deposit bid security EMD MSE exception",
    "mii": "make in India local content class I class II local supplier",
    "mii_allocation": "make in India quantity allocation split class I",
    "land_border": "land border country DPIIT registration rule 144",
    "pqr": "pre-qualification experience work order completion certificate",
    "nda": "non-disclosure agreement confidential drawings",
    "oem_tc": "original manufacturer test certificate quality",
    "participation": "eligible class local supplier manufacturer participation",
    "deviation": "loading criteria technical commercial deviation",
    "deviation_statement": "deviation or no-deviation statement",
    "document": "required bidder document",
}
_FAMILY_NEEDLES = {
    "pan": ("pan", "permanent account"),
    "gst": ("gst", "gstin", "goods and services tax"),
    "udyam_mse": ("mse", "micro and small", "udyam"),
    "startup_not_offered": ("startup", "dpiit"),
    "mse_relaxation_not_offered": ("mse", "micro and small"),
    "emd": ("emd", "earnest money", "bid security"),
    "mii": ("make in india", "local content", "class i", "class-i", "class 1"),
    "mii_allocation": ("allocation", "split", "make in india"),
    "land_border": ("land border", "dpiit", "144"),
    "pqr": ("pre-qualification", "prequalification", "pqr"),
    "nda": ("non-disclosure", "nda", "confidential"),
    "oem_tc": ("test certificate", "original manufacturer"),
    "participation": ("eligible", "class i", "class-i", "manufacturer"),
    "deviation": ("loading", "deviation"),
}


def _policy_id(tender: dict[str, Any]) -> str:
    return policy_id_for_profile(tender)


def _bid_system(tender: dict[str, Any]) -> str:
    return bid_system_kind(tender)


def _is_two_part(tender: dict[str, Any]) -> bool:
    return _bid_system(tender) == "two_part"


def _clause_map(policy_id: str) -> dict[str, str]:
    return _PROCESS.get(policy_id) or {}


def _book_name(policy_id: str) -> str:
    try:
        return str(load_corpus(policy_id).get("name") or policy_id)
    except Exception:
        return policy_id


def _clause(policy_id: str, clause_id: str) -> dict[str, str]:
    clause_id = str(clause_id or "").strip()
    if not clause_id:
        return {
            "policy_id": policy_id,
            "policy_name": _book_name(policy_id),
            "clause_id": "",
            "title": "",
            "quote": "",
        }
    try:
        corpus = load_corpus(policy_id)
    except Exception:
        corpus = {}
    for item in corpus.get("clauses") or []:
        if str(item.get("clause_id") or "") != clause_id:
            continue
        quote = normalize_ws(item.get("text") or "")
        if not quote:
            break
        return {
            "policy_id": policy_id,
            "policy_name": str(corpus.get("name") or policy_id),
            "clause_id": clause_id,
            "title": str(item.get("title") or ""),
            "quote": quote[:900],
        }
    return {
        "policy_id": policy_id,
        "policy_name": _book_name(policy_id),
        "clause_id": "",
        "title": "",
        "quote": "",
    }


def _subject_id(policy_id: str, family: str) -> str:
    table = _SUBJECT.get(policy_id) or {}
    return str(table.get(family) or "")


def _subject_clause(policy_id: str, family: str) -> dict[str, str]:
    return _clause(policy_id, _subject_id(policy_id, family))


def _emd_mse_clause(policy_id: str) -> dict[str, str]:
    return _clause(policy_id, _EMD_MSE_CLAUSE.get(policy_id) or "")


def _approvals(policy_id: str, clause_id: str, *, kind: str = "") -> list[dict[str, Any]]:
    rows = []
    for entry in approvals_for(policy_id, clause_id):
        action = str(entry.get("action") or "")
        lowered = action.lower()
        if kind == "technical" and "commercial" in lowered and "technical" not in lowered:
            continue
        if kind == "commercial" and "technical" in lowered and "commercial" not in lowered:
            continue
        rows.append(
            {
                "action": entry.get("action"),
                "authority_title": entry.get("authority_title"),
                "clause_id": entry.get("clause_id") or clause_id,
                "policy_id": entry.get("policy_id") or policy_id,
                "quote": entry.get("quote"),
            }
        )
    return rows


def _policy_clause_entry(policy_id: str, clause_id: str, role: str) -> dict[str, str] | None:
    grounded = _clause(policy_id, clause_id)
    if not grounded.get("clause_id") or not grounded.get("quote"):
        return None
    return {
        "role": role,
        "policy_id": grounded["policy_id"],
        "policy_name": grounded["policy_name"],
        "clause_id": grounded["clause_id"],
        "title": grounded["title"],
        "quote": grounded["quote"],
    }


def _append_policy_clause(
    clauses: list[dict[str, str]],
    policy_id: str,
    clause_id: str,
    role: str,
) -> dict[str, str] | None:
    entry = _policy_clause_entry(policy_id, clause_id, role)
    if entry and not any(
        row.get("clause_id") == entry["clause_id"] and row.get("role") == role
        for row in clauses
    ):
        clauses.append(entry)
    return entry


def _subject_catalog(policy_id: str) -> list[dict[str, str]]:
    try:
        corpus = load_corpus(policy_id)
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for item in corpus.get("clauses") or []:
        clause_id = str(item.get("clause_id") or "").strip()
        text = normalize_ws(item.get("text") or "")
        if not clause_id or not text or clause_id in _BLOCKED_CLAUSE_IDS:
            continue
        if not re.match(r"^[0-9A-Za-z]+(\.[0-9A-Za-z]+)*$", clause_id):
            continue
        rows.append(
            {
                "clause_id": clause_id,
                "title": str(item.get("title") or ""),
                "text": text[:900],
            }
        )
    return rows


def retrieve_subject_clauses(
    policy_id: str,
    card: dict[str, Any],
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Look up the book. Process clauses (8.6/8.7/…) are not in this list."""
    family = str(card.get("family") or "")
    query = " ".join(
        [
            _FAMILY_QUERY.get(family, ""),
            str(card.get("title") or ""),
            str(card.get("missing_paper") or ""),
            str(card.get("reason") or "")[:240],
        ]
    )
    catalog = _subject_catalog(policy_id)
    if not catalog:
        return []
    texts = [f"{row['clause_id']} {row['title']} {row['text']}" for row in catalog]
    ranked = lexical_rank(query, texts)[:limit]
    return [catalog[index] for index in ranked]


def _needles_hit(family: str, text: str) -> bool:
    blob = text.casefold()
    needles = _FAMILY_NEEDLES.get(family) or ()
    if not needles:
        return False
    return any(needle in blob for needle in needles)


def _attach_governing_clause(card: dict[str, Any], grounded: dict[str, str]) -> None:
    card["policy"] = {
        "policy_id": grounded.get("policy_id") or card.get("policy", {}).get("policy_id"),
        "clause_id": grounded["clause_id"],
        "title": grounded.get("title") or "",
        "quote": grounded.get("quote") or "",
    }
    skip = {"ask_once", "single_part_closed", "deviation_loading"}
    keep = {"mse_exemption"}
    for door in card.get("doors") or []:
        if not isinstance(door, dict) or door.get("id") in skip:
            continue
        if door.get("id") in keep and door.get("clause_id"):
            continue
        door["clause_id"] = grounded["clause_id"]
        door["clause_title"] = grounded.get("title") or ""
        door["clause_quote"] = grounded.get("quote") or ""
        door["policy_id"] = grounded.get("policy_id") or door.get("policy_id")
        door["policy_name"] = grounded.get("policy_name") or door.get("policy_name")


def apply_governing_clauses(payload: dict[str, Any]) -> dict[str, Any]:
    """Clauses are attached deterministically while each card is assembled."""
    return payload


_GOVERNING_INSTRUCTIONS = (
    "Each card has candidate clauses copied from the BHEL policy book. "
    "Pick at most one clause_id from THAT card's candidates. "
    "The clause text must actually require or regulate this missing paper or treatment. "
    "A clause that merely mentions tenders, documents, or evaluation is not a match. "
    "If the miss is MSE preference / Udyam and the clause is only about EMD, that is not a match. "
    "If none of the candidates govern this miss, return clause_id as an empty string. "
    "Never invent a clause_id. Never pick a number that is not in candidates."
)


def _fill_governing_batch(
    batch: list[tuple[dict[str, Any], list[dict[str, str]]]],
    policy_id: str,
) -> None:
    cards_payload = []
    allowed: dict[str, set[str]] = {}
    by_card = {}
    for card, candidates in batch:
        criterion_id = str(card.get("criterion_id") or "")
        allowed[criterion_id] = {row["clause_id"] for row in candidates}
        by_card[criterion_id] = (card, candidates)
        cards_payload.append(
            {
                "criterion_id": criterion_id,
                "family": card.get("family"),
                "title": card.get("title"),
                "missing_paper": card.get("missing_paper"),
                "reason": card.get("reason"),
                "candidates": candidates,
            }
        )
    try:
        result = llm_runtime.llm_json(
            "You look up BHEL Purchase/Works Policy. Return only JSON. Never invent a clause.",
            {
                "task": "bid_governing_clause",
                "section": "policy_lookup",
                "instructions": _GOVERNING_INSTRUCTIONS,
                "cards": cards_payload,
                "json_shape": {
                    "cards": [
                        {
                            "criterion_id": "same criterion_id",
                            "clause_id": "id from that card's candidates, or empty",
                        }
                    ]
                },
            },
            max_output_tokens=max(400, 80 * len(cards_payload)),
        )
    except Exception:
        logger.exception("bid governing-clause LLM failed")
        return
    rows = result.get("cards") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        criterion_id = str(row.get("criterion_id") or "")
        picked = str(row.get("clause_id") or "").strip()
        if not criterion_id or criterion_id not in by_card:
            continue
        if not picked or picked not in allowed.get(criterion_id, set()):
            continue
        card, _candidates = by_card[criterion_id]
        grounded = _clause(policy_id, picked)
        if grounded.get("clause_id") != picked or not grounded.get("quote"):
            continue
        if not _needles_hit(str(card.get("family") or ""), grounded["quote"]):
            continue
        _attach_governing_clause(card, grounded)


__all__ = [
    "PURCHASE",
    "WORKS",
    "logger",
    "_PROCESS",
    "_SUBJECT",
    "_EMD_MSE_CLAUSE",
    "_PROCESS_CLAUSE_IDS",
    "_BLOCKED_CLAUSE_IDS",
    "_FAMILY_QUERY",
    "_FAMILY_NEEDLES",
    "_policy_id",
    "_bid_system",
    "_is_two_part",
    "_clause_map",
    "_book_name",
    "_clause",
    "_subject_id",
    "_subject_clause",
    "_emd_mse_clause",
    "_approvals",
    "_policy_clause_entry",
    "_append_policy_clause",
    "_subject_catalog",
    "retrieve_subject_clauses",
    "_needles_hit",
    "_attach_governing_clause",
    "apply_governing_clauses",
    "_GOVERNING_INSTRUCTIONS",
    "_fill_governing_batch",
]
