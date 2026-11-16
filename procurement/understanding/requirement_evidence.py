"""Deterministic evidence matching for reviewed tender requirements."""

from __future__ import annotations

import math
import re
from typing import Any

from procurement.understanding.requirement_evidence_catalog import (
    _ATOMIC_DOCUMENT_TYPES,
    _EVIDENCE_MATCH_STOPWORDS,
    _EVIDENCE_TOKEN_ALIASES,
    _FIELD_EVIDENCE_SPECS,
    _evidence_match_tokens,
)
from procurement.understanding.requirement_semantics import (
    _COMMERCIAL_CONTEXT_FIELDS,
    _CONDITIONAL_POLICY_FIELDS,
    _SHARED_CALL_BUDGET_NOTE,
    _atomic_requirement_values,
    _requirement_semantic_key,
    _requirement_semantics,
    _shared_call_budget_exhausted,
    requirement_semantics_for_reviewed_atomic_field,
)
from procurement.understanding.requirement_proof_specs import (
    _named_document_spec,
    _pqr_named_proofs,
    _proof_document_type,
    _reviewed_document_type,
    evidence_requirements_for_field,
)

_COMPOSITE_REQUIREMENT_FIELDS = {
    "allowed_legal_entities",
    "debarment_and_blacklisting",
    "financial_capacity",
    "minimum_industry_experience",
    "permitted_participant_structure",
    "purchase_preference_and_allocation",
    "similar_project_qualification",
    "statutory_registrations_required",
}
_ATOMIC_REQUIREMENT_ORDER = {
    "bid_security_and_fees": (
        ("emd", "earnest money", "bid security"),
        ("tender fee", "document fee", "processing fee"),
        ("performance bank guarantee", "pbg"),
    ),
    "statutory_registrations": (
        ("pan", "permanent account number"),
        ("gstin", "gst registration", "goods and services tax"),
        ("cin", "company identification number", "incorporation"),
        ("udyam", "msme registration", "mse registration"),
        ("epf", "provident fund"),
        ("esic", "employee state insurance"),
        ("labour licence", "labor license"),
        ("export licence", "export license"),
    ),
    "contractual_declarations": (
        ("integrity pact",),
        ("land border",),
        ("local content", "make in india"),
        ("no deviation", "deviation declaration"),
    ),
}


def _requirement_values(field_key: str, value: Any) -> list[str]:
    values = []
    seen: set[str] = set()
    for item in _atomic_requirement_values(value):
        marker = " ".join(item.casefold().split())
        if marker and marker not in seen:
            seen.add(marker)
            values.append(item)
    if field_key in _COMPOSITE_REQUIREMENT_FIELDS and len(values) > 1:
        return ["; ".join(values)]
    order = _ATOMIC_REQUIREMENT_ORDER.get(field_key)
    if not order:
        return values

    def sort_key(text: str) -> tuple[int, str]:
        normalized = " ".join(text.casefold().split())
        for index, aliases in enumerate(order):
            if any(alias in normalized for alias in aliases):
                return index, normalized
        return len(order), normalized

    return sorted(values, key=sort_key)


def _criterion_suffix(field_key: str, text: str, index: int) -> str:
    """Keep generated IDs stable when extracted wording changes slightly."""
    if index == 1:
        return ""
    normalized = " ".join(text.casefold().split())
    named_suffixes = {
        "financial_capacity": (
            ("net-worth", ("net worth", "networth")),
            ("turnover", ("turnover", "annual revenue")),
            ("solvency", ("solvency",)),
            ("profitability", ("profit",)),
        ),
        "statutory_registrations": (
            ("pan", ("pan", "permanent account number")),
            ("gstin", ("gstin", "gst registration", "goods and services tax")),
            ("cin", ("cin", "company identification number", "incorporation")),
            ("udyam", ("udyam", "msme registration", "mse registration")),
            ("epf", ("epf", "provident fund")),
            ("esic", ("esic", "employee state insurance")),
            ("labour-licence", ("labour licence", "labor license")),
            ("export-licence", ("export licence", "export license")),
        ),
        "contractual_declarations": (
            ("integrity-pact", ("integrity pact",)),
            ("land-border", ("land border",)),
            ("local-content", ("local content", "make in india")),
            ("no-deviation", ("no deviation", "deviation declaration")),
        ),
        "digital_signing": (
            ("authorization", ("authorised signatory", "authorized signatory", "power of attorney")),
            ("digital-signature", ("digital signature", "dsc")),
        ),
    }
    for suffix, aliases in named_suffixes.get(field_key, ()):
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized) for alias in aliases):
            return f"-{suffix}"
    label = text.partition(":")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")[:32]
    return f"-{slug or index}"


def _evidence_for_atomic_value(
    field_key: str,
    evidence: list[dict[str, Any]],
    atomic_value: str,
    *,
    composite: bool,
    source_references: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    preferred_types = {
        str(spec.get("document_type") or "")
        for spec in _FIELD_EVIDENCE_SPECS.get(field_key, [])
        if str(spec.get("document_type") or "")
    }
    typed = [
        dict(item)
        for item in evidence
        if not preferred_types or str(item.get("document_type") or "") in preferred_types
    ]
    if typed:
        evidence = typed
    if composite:
        return [dict(item) for item in evidence]

    atomic_normalized = " ".join(atomic_value.casefold().split())
    if field_key == "digital_signing":
        routes = (
            (
                ("authorised signatory", "authorized signatory", "power of attorney"),
                ("authorised signatory", "authorized signatory", "power of attorney", "statutory auditor"),
            ),
            (("tax invoice", "supply order invoice"), ("tax invoice", "supply order invoice")),
        )
        for requirement_markers, evidence_markers in routes:
            if not any(marker in atomic_normalized for marker in requirement_markers):
                continue
            routed = [
                dict(item)
                for item in evidence
                if any(
                    marker
                    in " ".join(
                        (
                            str(item.get("document_name") or ""),
                            str(item.get("acceptance_rule") or ""),
                        )
                    ).casefold()
                    for marker in evidence_markers
                )
            ]
            if routed:
                return routed

    value_tokens = _evidence_match_tokens(atomic_value)
    source_text = " ".join(
        str(reference.get("quote") or "") for reference in source_references or []
    )
    source_tokens = _evidence_match_tokens(source_text)
    source_normalized = " ".join(source_text.casefold().split())
    scored: list[tuple[int, int, bool, dict[str, Any]]] = []
    for raw in evidence:
        item = dict(raw)
        acceptance_rule = str(item.get("acceptance_rule") or "")
        if acceptance_rule.casefold().startswith("evidence must satisfy the reviewed "):
            acceptance_rule = ""
        searchable = " ".join(
            [
                str(item.get("document_name") or ""),
                str(item.get("document_type") or ""),
                " ".join(str(check) for check in item.get("checks") or []),
                acceptance_rule,
            ]
        )
        evidence_tokens = _evidence_match_tokens(searchable)
        direct_overlap = value_tokens & evidence_tokens
        source_overlap = source_tokens & evidence_tokens
        direct_score = sum(4 if len(token) <= 6 else 3 for token in direct_overlap)
        evidence_normalized = " ".join(searchable.casefold().split())
        strong_route = False
        if (
            any(
                marker in atomic_normalized
                for marker in (
                    "hsse",
                    "risk assessment",
                    "fire fighting",
                    "firefighting",
                    "ppe",
                    "housekeeping",
                    "safety",
                    "quality control check",
                    "marks",
                    "score",
                )
            )
            and "checklist" in evidence_normalized
        ):
            direct_score += 12
            strong_route = True
        if (
            any(marker in source_normalized for marker in ("marks", "scored", "score awarded"))
            and "checklist" in evidence_normalized
        ):
            direct_score += 12
            strong_route = True
        if (
            any(marker in atomic_normalized for marker in ("calibration", "calibrated"))
            and any(marker in evidence_normalized for marker in ("calibration", "test certificate"))
        ):
            direct_score += 12
            strong_route = True
        source_score = len(source_overlap)
        scored.append((direct_score, source_score, strong_route, item))
    strongly_routed = [item for _direct, _source, strong, item in scored if strong]
    if strongly_routed:
        return strongly_routed
    best_direct = max((score for score, _source, _strong, _item in scored), default=0)
    use_direct = best_direct > 0
    use_source = not use_direct and bool(value_tokens & source_tokens)
    best = (
        best_direct
        if use_direct
        else max((source for _score, source, _strong, _item in scored), default=0)
        if use_source
        else 0
    )
    if best <= 0:
        return []
    if use_direct:
        # Candidate proofs represent separate obligations, not competitors.
        # Keep every independently relevant proof (for example Water, Air and
        # Factory licences) while excluding unrelated documents.
        threshold = 3 if len(value_tokens) > 1 else 1
    else:
        threshold = 1 if len(source_tokens) <= 2 else max(2, math.ceil(best * 0.4))
    return [
        item
        for direct_score, source_score, _strong, item in scored
        if (direct_score if use_direct else source_score) >= threshold
    ]


def _default_evidence_for_atomic_value(field_key: str, atomic_value: str) -> dict[str, Any]:
    """Create one precise search target instead of attaching an unrelated document bundle."""
    label = re.sub(r"\s+", " ", atomic_value).strip().rstrip(".")
    if len(label) > 180:
        label = f"{label[:177].rstrip()}..."
    return {
        "evidence_id": f"{field_key}-E1",
        "document_name": f"Bidder evidence for: {label}",
        "document_type": _ATOMIC_DOCUMENT_TYPES.get(field_key, "unknown"),
        "required": True,
        "checks": ["presence", "tender_requirement_match"],
        "acceptance_rule": "Evidence must satisfy this exact reviewed tender requirement.",
        "missing_effect": "manual_review",
    }


def evidence_for_reviewed_atomic_field(
    field_key: str,
    title: str,
    required_documents: list[str],
    atomic_value: str,
    source_references: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Recover precise evidence for an already-approved atomic criterion."""
    candidates = evidence_requirements_for_field(field_key, title, required_documents)
    routed = _evidence_for_atomic_value(
        field_key,
        candidates,
        atomic_value,
        composite=False,
        source_references=source_references,
    )
    if routed:
        return routed
    named_proofs = _pqr_named_proofs(atomic_value, source_references)
    if named_proofs:
        return [
            {
                "evidence_id": f"{field_key}-E{index}",
                "document_name": str(spec["document_name"]),
                "document_type": str(spec.get("document_type") or "unknown"),
                "required": bool(spec.get("required", True)),
                "checks": [str(check) for check in spec.get("checks") or ["presence", "tender_requirement_match"]],
                "acceptance_rule": str(
                    spec.get("acceptance_rule")
                    or "Evidence must satisfy the named PQR/tender proof."
                ),
                "missing_effect": "manual_review",
            }
            for index, spec in enumerate(named_proofs, start=1)
        ]
    return []


def evidence_groups_for_reviewed_atomic_field(
    field_key: str,
    atomic_value: str,
    pass_rule: str,
    evidence: list[dict[str, Any]],
) -> list[list[str]]:
    """Expose the compiler's AND-of-OR grouping for runtime rehydration."""
    return _evidence_groups(field_key, atomic_value, pass_rule, evidence)


def _source_references_for_atomic_value(
    references: list[dict[str, Any]],
    atomic_value: str,
) -> list[dict[str, Any]]:
    if len(references) <= 1:
        return [dict(reference) for reference in references]

    wanted = _evidence_match_tokens(atomic_value)
    acronyms = {
        token
        for token in re.findall(r"\b[A-Z0-9]{2,}\b", atomic_value)
        if any(char.isalpha() for char in token)
    }
    scored = [
        (
            len(wanted & _evidence_match_tokens(str(reference.get("quote") or ""))),
            dict(reference),
        )
        for reference in references
    ]
    best = max((score for score, _reference in scored), default=0)
    if best <= 0:
        # A visual verifier may ground a finding to an exact page without
        # transcribing text. Preserve that honest page citation when the
        # field also contains a different quoted reference that does not
        # match this atomic requirement.
        return [
            reference
            for _score, reference in scored
            if not str(reference.get("source_id") or "").strip()
            and not str(reference.get("quote") or "").strip()
            and (
                str(reference.get("page_id") or "").strip()
                or reference.get("page") not in (None, "")
            )
        ]
    threshold = 1 if len(wanted) <= 2 else max(2, math.ceil(best * 0.35))
    # An acronym from the atomic value (PAN, GSTIN, EMD) is decisive even when
    # it is the only shared token: sources abbreviate long statutory names.
    return [
        reference
        for score, reference in scored
        if score >= threshold
        or (
            acronyms
            and acronyms
            & set(re.findall(r"\b[A-Z0-9]{2,}\b", str(reference.get("quote") or "")))
        )
    ]


def _context_for_atomic_value(
    values: list[str],
    atomic_value: str,
    atomic_count: int,
    *,
    shared_relief: bool = False,
) -> list[str]:
    """Keep only conditions/exceptions that belong to this atomic obligation."""
    if atomic_count <= 1:
        return list(values)
    target_tokens = _evidence_match_tokens(atomic_value)
    generic_tokens = {"applicable", "claim", "claimed", "condition", "required"}
    decisive_tokens = {"pan", "gstin", "nda", "emd", "pbg", "epf", "esic", "udyam"}
    relief_tokens = {"exemption", "waiver", "relaxation"}
    scoped: list[str] = []
    for value in values:
        tokens = _evidence_match_tokens(value)
        overlap = tokens & target_tokens
        if (
            len(overlap) >= 2
            or overlap & decisive_tokens
            or not (tokens - generic_tokens)
            or shared_relief and tokens & relief_tokens
        ):
            scoped.append(value)
    return scoped


def _atomic_value_has_source_table_ambiguity(
    atomic_value: str,
    field: dict[str, Any],
) -> bool:
    searchable = atomic_value.casefold()
    anomalies = (field.get("audit") or {}).get("source_table_ambiguities") or []
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            continue
        label = str(anomaly.get("label") or "").casefold()
        values = [value.strip() for value in str(anomaly.get("values") or "").split(",")]
        if not label or not values or not re.search(rf"\b{re.escape(label)}\b", searchable):
            continue
        if all(
            value and re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d.,])", searchable)
            for value in values
        ):
            return True
    return False


def _evidence_groups(
    field_key: str,
    atomic_value: str,
    pass_rule: str,
    evidence: list[dict[str, Any]],
) -> list[list[str]]:
    """Return explicit AND-of-OR evidence groups for the evaluator."""
    required = [item for item in evidence if item.get("required", True)]
    evidence_ids = [str(item.get("evidence_id") or "") for item in required]
    evidence_ids = [evidence_id for evidence_id in evidence_ids if evidence_id]
    if not evidence_ids:
        return []
    rule_text = f" {atomic_value} {pass_rule} ".casefold()
    text = rule_text + " ".join(
        str(item.get("document_name") or "") for item in required
    )
    normalized = f" {text.casefold()} "
    alternatives = field_key == "bid_security_and_fees" and any(
        term in normalized for term in (" emd ", " earnest money ", " bid security ")
    ) and any(term in normalized for term in (" or ", " exemption ", " either "))
    alternatives = alternatives or any(term in rule_text for term in (" any one ", " either "))
    alternatives = alternatives or (len(evidence_ids) == 2 and " or " in rule_text)
    groups = [evidence_ids] if alternatives and len(evidence_ids) > 1 else [[item] for item in evidence_ids]
    return _synchronize_evidence_groups(groups, required)


def _synchronize_evidence_groups(
    groups: list[list[str]],
    evidence: list[dict[str, Any]],
) -> list[list[str]]:
    """Keep raw groups aligned with the current required evidence IDs.

    Reviewer-selected optional evidence is intentionally excluded from groups;
    only current required evidence can impose an evaluation obligation.
    """
    current_ids: list[str] = []
    current_set: set[str] = set()
    for item in evidence:
        if not item.get("required", True):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if evidence_id and evidence_id not in current_set:
            current_ids.append(evidence_id)
            current_set.add(evidence_id)

    synchronized: list[list[str]] = []
    seen: set[str] = set()
    for group in groups:
        current_group: list[str] = []
        group_seen: set[str] = set()
        for evidence_id in group:
            if (
                evidence_id in current_set
                and evidence_id not in seen
                and evidence_id not in group_seen
            ):
                current_group.append(evidence_id)
                group_seen.add(evidence_id)
        if current_group:
            synchronized.append(current_group)
            seen.update(current_group)
    synchronized.extend(
        [[evidence_id]]
        for evidence_id in current_ids
        if evidence_id not in seen
    )
    return synchronized


__all__ = [
    "_COMPOSITE_REQUIREMENT_FIELDS",
    "_ATOMIC_REQUIREMENT_ORDER",
    "_ATOMIC_DOCUMENT_TYPES",
    "_EVIDENCE_MATCH_STOPWORDS",
    "_EVIDENCE_TOKEN_ALIASES",
    "_FIELD_EVIDENCE_SPECS",
    "_evidence_match_tokens",
    "_COMMERCIAL_CONTEXT_FIELDS",
    "_CONDITIONAL_POLICY_FIELDS",
    "_SHARED_CALL_BUDGET_NOTE",
    "_atomic_requirement_values",
    "_requirement_semantic_key",
    "_requirement_semantics",
    "_shared_call_budget_exhausted",
    "requirement_semantics_for_reviewed_atomic_field",
    "_named_document_spec",
    "_pqr_named_proofs",
    "_proof_document_type",
    "_reviewed_document_type",
    "evidence_requirements_for_field",
    "_requirement_values",
    "_criterion_suffix",
    "_evidence_for_atomic_value",
    "_default_evidence_for_atomic_value",
    "evidence_for_reviewed_atomic_field",
    "evidence_groups_for_reviewed_atomic_field",
    "_source_references_for_atomic_value",
    "_context_for_atomic_value",
    "_atomic_value_has_source_table_ambiguity",
    "_evidence_groups",
    "_synchronize_evidence_groups",
]
