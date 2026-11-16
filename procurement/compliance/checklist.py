"""Compile the short, triggered compliance checklist for one governing book."""

from __future__ import annotations

from typing import Any

from procurement.compliance.corpus import is_tender_facing, load_applicability, load_corpus
from procurement.compliance.profile import trigger_is_on

# Bid-handling / custody / interest-on-EMD: BHEL internal or GeM-covered.
# They must not appear as "tender forgot to restate this".
_SKIP_CLAUSES = {
    "bhel_purchase_policy": {
        "8.4",
        "8.5",
        "8.8",
        "8.8.1",
        "8.8.2",
        "8.8.3",
        "8.8.4",
        "8.9",
        "8.10",
        "5A.6",
        "5B.6",
        "10.3",
    },
    "bhel_works_policy": {
        "9.4",
        "9.5",
        "9.8",
        "9.8.1",
        "9.8.2",
        "9.8.3",
        "9.8.4",
        "9.9",
        "9.10",
        "5.1.6",
        "5.2.6",
        "11.3",
    },
}

# Always judge these when the book governs, unless skipped above.
_ALWAYS = {
    "bhel_purchase_policy": {
        "8.1",
        "8.2",
        "5A.1",
        "5A.2",
        "9.0",
        "22.1",
    },
    "bhel_works_policy": {
        "9.1",
        "9.2",
        "5.1.1",
        "5.2.1",
        "10.0",
        "24.1",
    },
}


def compile_active_checks(
    policy_id: str,
    profile: dict[str, Any],
    *,
    haystack: str = "",
) -> list[dict[str, Any]]:
    """Return only the checks this tender's structure actually triggers."""
    corpus = load_corpus(policy_id)
    applicability = load_applicability(policy_id)
    skip = _SKIP_CLAUSES.get(policy_id, set())
    always = _ALWAYS.get(policy_id, set())
    active: list[dict[str, Any]] = []
    for clause in corpus.get("clauses") or []:
        clause_id = str(clause.get("clause_id") or "")
        if not clause_id or clause_id in skip:
            continue
        record = applicability.get(clause_id) or {}
        mode = str(record.get("mode") or "unconditional")
        if mode == "internal_process" and clause_id not in always:
            continue
        if clause_id not in always and not is_tender_facing(clause, policy_id):
            continue
        reason = ""
        if clause_id in always:
            reason = "must-state for this book"
        elif mode == "conditional":
            trigger = str(record.get("trigger") or "")
            if not trigger_is_on(trigger, profile, haystack):
                continue
            reason = f"tender uses: {trigger}"
        elif mode == "unconditional":
            # Soft unconditional items (10.1 delivered cost, consultant) only
            # when the tender itself raises the subject.
            if clause_id in {"10.1", "11.1"} and not profile.get("evaluation_stated"):
                continue
            if clause_id in {"26.0", "29.0"} and not profile.get("consultant"):
                continue
            if clause_id not in always:
                continue
            reason = "must-state for this book"
        else:
            continue
        item = dict(clause)
        item["applicability"] = record
        item["activation"] = reason
        item["policy_id"] = policy_id
        item["policy_name"] = corpus.get("name")
        active.append(item)
    return active
