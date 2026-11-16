"""Approval-authority (delegation of powers) lookups for policy findings.

The approval map is a committed data artifact (config/policy/approval_map.v1.json)
built once by scripts/build_approval_map.py from the versioned policy corpora.
At runtime it answers: "to record an exception for this clause, whose approval
(title/rank) does the policy require?"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_MAP_PATH = _ROOT / "config" / "policy" / "approval_map.v1.json"

_cache: dict[tuple[str, str], list[dict[str, Any]]] | None = None


def _load() -> dict[tuple[str, str], list[dict[str, Any]]]:
    global _cache
    if _cache is None:
        if not _MAP_PATH.is_file():
            raise ValueError("policy approval map is missing")
        payload = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "approval-map.v1":
            raise ValueError("unsupported policy approval-map schema")
        from procurement.compliance.corpus import load_corpus

        known_clauses = {
            policy_id: {
                str(clause.get("clause_id") or ""): " ".join(str(clause.get("text") or "").split())
                for clause in load_corpus(policy_id).get("clauses") or []
            }
            for policy_id in ("bhel_purchase_policy", "bhel_works_policy")
        }
        cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in payload.get("entries") or []:
            key = (str(entry.get("policy_id") or ""), str(entry.get("clause_id") or ""))
            if key[0] not in known_clauses or key[1] not in known_clauses[key[0]]:
                raise ValueError(f"approval map references unknown policy clause: {key}")
            if not all(str(entry.get(field) or "").strip() for field in ("action", "authority_title", "quote")):
                raise ValueError(f"approval map entry is incomplete: {key}")
            quote = " ".join(str(entry.get("quote") or "").split())
            if quote not in known_clauses[key[0]][key[1]]:
                raise ValueError(f"approval map quote is not grounded in policy clause: {key}")
            action_text = str(entry.get("action") or "").casefold()
            quote_text = quote.casefold()
            for subject in ("technical deviation", "commercial deviation"):
                if subject in action_text and subject not in quote_text:
                    raise ValueError(f"approval map action/quote mismatch for {subject}: {key}")
            cache.setdefault(key, []).append(
                {
                    "action": entry.get("action"),
                    "authority_title": entry.get("authority_title"),
                    "quote": entry.get("quote"),
                    "policy_id": entry.get("policy_id"),
                    "clause_id": entry.get("clause_id"),
                }
            )
        _cache = cache
    return _cache


def approvals_for(policy_id: str, clause_id: str) -> list[dict[str, Any]]:
    """Approval rules for one policy clause.

    Exact clause only, plus a short related-clause list (5A.1 slab → 5A.3
    waiver). Do not pull every 5A.* row: 5A.5 (release EMD later) must not
    appear on a 5A.3 (waive EMD in the NIT) card.
    """
    if not clause_id:
        return []
    from procurement.compliance.officer_language import related_approval_clauses

    index = _load()
    wanted = {clause_id, *related_approval_clauses(policy_id, clause_id)}
    results: list[dict[str, Any]] = []
    for clause in wanted:
        results.extend(index.get((policy_id, clause), []))
    return results


def approvals_for_finding(finding: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve approval rules for a compliance finding via its policy reference
    (or the policy prefix of its finding_id) and clause id."""
    policy_id = ""
    reference = finding.get("policy_reference")
    if isinstance(reference, dict):
        policy_id = str(reference.get("policy_id") or "")
    if not policy_id:
        finding_id = str(finding.get("finding_id") or "")
        if ":" in finding_id:
            policy_id = finding_id.split(":", 1)[0]
    clause_id = str(finding.get("clause_id") or "")
    if not policy_id or not clause_id:
        return []
    return approvals_for(policy_id, clause_id)


# Bid-evaluation alternatives: when required evidence is missing or fails, the
# policy-allowed alternative and the title that must approve it. Keys are
# keyword sets matched against the criterion's text; values are clause ids per
# policy that carry the alternative + approval rule.
_CRITERION_ALTERNATIVES = [
    {
        "keywords": ("emd", "bid security", "earnest money"),
        "label": "EMD / bid security",
        "clauses": {"bhel_purchase_policy": ["5A.3"], "bhel_works_policy": ["5.1.3"]},
    },
    {
        "keywords": ("performance security", "performance bank guarantee", "security deposit", "bank guarantee"),
        "label": "performance security / bank guarantee",
        "clauses": {"bhel_purchase_policy": ["5B.3"], "bhel_works_policy": ["5.2.3"]},
    },
    {
        "keywords": ("late tender", "late offer", "late bid"),
        "label": "late offers",
        "clauses": {"bhel_purchase_policy": ["9.0"], "bhel_works_policy": ["10.0"]},
    },
    {
        "keywords": ("deviation", "loading criteria"),
        "label": "deviation acceptance",
        "clauses": {"bhel_purchase_policy": ["8.7"], "bhel_works_policy": ["9.7"]},
    },
]


def alternatives_for_criterion(criterion: dict[str, Any]) -> list[dict[str, Any]]:
    """Policy-allowed alternatives (with approving title) for a failed or
    missing-evidence criterion. Empty when the policy offers no alternative."""
    text = " ".join(
        str(criterion.get(key) or "")
        for key in ("title", "requirement", "required_proof", "family", "reason")
    ).lower()
    results: list[dict[str, Any]] = []
    for rule in _CRITERION_ALTERNATIVES:
        if not any(keyword in text for keyword in rule["keywords"]):
            continue
        for policy_id, clause_ids in rule["clauses"].items():
            for clause_id in clause_ids:
                for entry in approvals_for(policy_id, clause_id):
                    results.append({**entry, "subject": rule["label"]})
    return results
