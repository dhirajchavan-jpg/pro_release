"""Requirement labels and matrix construction from reviewed tender fields."""

from __future__ import annotations

from typing import Any

from procurement.understanding.requirement_evidence import (
    _COMPOSITE_REQUIREMENT_FIELDS,
    _atomic_value_has_source_table_ambiguity,
    _context_for_atomic_value,
    _criterion_suffix,
    _default_evidence_for_atomic_value,
    _evidence_for_atomic_value,
    _evidence_groups,
    _requirement_semantics,
    _requirement_semantic_key,
    _requirement_values,
    _source_references_for_atomic_value,
    evidence_requirements_for_field,
)

from .tender_field_projection_fields import field_entries
from .tender_field_projection_provenance import (
    _attach_matrix_provenance,
    _canonical_source_references,
    _canonical_source_units,
)


_REQUIREMENT_FIELDS = {
    "emd_amount_inr": ("EMD", "commercial"),
    "mse_startup_relaxation": ("MSE/startup relaxation", "compliance"),
    "msme_preference_applicable": ("MSME preference", "compliance"),
    "make_in_india_preference": ("Make in India preference", "compliance"),
    "allowed_legal_entities": ("Allowed legal entities", "eligibility"),
    "joint_venture_allowed": ("Joint venture eligibility", "eligibility"),
    "statutory_registrations_required": ("Statutory registrations", "eligibility"),
    "non_blacklisting_affidavit": ("Non-blacklisting affidavit", "compliance"),
    "local_content_declaration": ("Local-content declaration", "compliance"),
    "integrity_pact_required": ("Integrity Pact", "compliance"),
    "min_turnover_req_inr": ("Minimum turnover", "financial"),
    "net_worth_requirement": ("Net-worth requirement", "financial"),
    "min_industry_experience_years": ("Minimum industry experience", "eligibility"),
    "similar_past_projects_count": ("Similar-project experience", "eligibility"),
    "similar_works_milestones": ("Similar-work qualification", "eligibility"),
    "cmmi_level_required": ("CMMI certification", "technical"),
    "iso_it_certifications": ("ISO certifications", "technical"),
    "cert_in_audit_compliance": ("CERT-In audit compliance", "technical"),
    "stqc_certification_required": ("STQC certification", "technical"),
    "meity_cloud_hosting_mandate": ("MeitY cloud-hosting mandate", "technical"),
    "data_localization_clause": ("Data-localization requirement", "technical"),
    "cyber_security_framework_adherence": ("Cyber-security framework", "technical"),
    "technical_infrastructure_proof": ("Technical infrastructure proof", "technical"),
    "bid_security_and_fees": ("Bid security and fees", "commercial"),
    "mse_startup_policy": ("MSE and startup policy", "compliance"),
    "make_in_india_policy": ("Make in India policy", "compliance"),
    "evaluation_scoring": ("Evaluation scoring", "technical"),
    "purchase_preference_and_allocation": ("Purchase preference and allocation", "commercial"),
    "permitted_participant_structure": ("Permitted participant structure", "eligibility"),
    "statutory_registrations": ("Statutory registrations", "eligibility"),
    "digital_signing": ("Digital signing and authorization", "compliance"),
    "debarment_and_blacklisting": ("Debarment and blacklisting", "compliance"),
    "contractual_declarations": ("Contractual declarations", "compliance"),
    "financial_capacity": ("Financial capacity", "financial"),
    "minimum_industry_experience": ("Minimum industry experience", "eligibility"),
    "similar_project_qualification": ("Similar-project qualification", "eligibility"),
    "third_party_certification_assurance": ("Third-party certification and assurance", "technical"),
    "government_audit_testing_empanelment": ("Government audit, testing, and empanelment", "technical"),
    "hosting_and_data_residency": ("Hosting and data-residency requirement", "technical"),
    "software_ownership_and_escrow": ("Software ownership and escrow", "technical"),
    "cybersecurity_controls": ("Cybersecurity controls", "technical"),
    "regulatory_and_statutory_obligations": ("Regulatory and statutory obligations", "compliance"),
    "delivery_capability_and_infrastructure": ("Delivery capability and infrastructure", "technical"),
    "technology_constraints": ("Technology constraints", "technical"),
    "solution_capabilities": ("Solution capabilities", "technical"),
}


def requirement_matrix_from_fields(
    understanding: dict[str, Any],
    units: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_ledger = understanding.get("source_obligations")
    if source_ledger is not None:
        from copy import deepcopy
        from procurement.evaluation.contracts import normalize_contract

        if not isinstance(source_ledger, dict) or not isinstance(source_ledger.get("items"), list):
            raise ValueError("Invalid source-obligation ledger; refusing summary-field fallback")
        coverage = source_ledger.get("coverage")
        expected = {key for key, unit in units.items()
                    if unit.get("unit_type") != "anchor_fact" and str(unit.get("text") or "").strip()}
        if not isinstance(coverage, list) or any(not isinstance(row, dict) for row in coverage):
            raise ValueError("Source-obligation ledger is missing coverage")
        accounted = [row.get("source_id") for row in coverage]
        if len(accounted) != len(set(accounted)) or set(accounted) != expected:
            raise ValueError("Source-obligation ledger does not cover the current tender sources")
        from procurement.understanding.source_context import source_fingerprint
        if source_ledger.get("source_fingerprint") != source_fingerprint(units):
            raise ValueError("Source-obligation ledger is stale or lacks source identity; regenerate from current tender sources")
        gaps = []
        for row in coverage:
            if row.get("disposition") != "needs_review":
                continue
            unit = units.get(row["source_id"], {})
            gaps.append({"source_id": row["source_id"], "requirement": row["reason"],
                         "source_quote": unit.get("text", ""), "document_id": unit.get("document"),
                         "page": unit.get("page")})
        for item in source_ledger["items"]:
            if (item.get("applicability") or {}).get("state") == "unknown":
                gaps.append({"criterion_id": item["criterion_id"],
                             "requirement": "Resolve applicability: " + item["requirement"],
                             "source_quote": item.get("source_quote", ""),
                             "source_references": deepcopy(item.get("source_references", []))})
        return {
            "items": normalize_contract(deepcopy(source_ledger["items"])),
            "unautomated_requirements": gaps,
            "extensions": {"source_obligations": {"version": source_ledger.get("version"),
                            "status": source_ledger.get("status"), "coverage": deepcopy(source_ledger.get("coverage", []))}},
            "planning_notes": ["Checklist derived from source-level obligations; summary fields are not its coverage boundary."],
        }
    canonical_units = _canonical_source_units(units)
    items = []
    criterion_ids: set[str] = set()
    for field in field_entries(understanding):
        key = str(field.get("key") or "")
        if key not in _REQUIREMENT_FIELDS:
            continue
        if field.get("status") in {"not_found", "not_applicable"} and field.get("audit", {}).get("verified") is True:
            continue
        title, category = _REQUIREMENT_FIELDS[key]
        evidence = evidence_requirements_for_field(
            key,
            title,
            list(field.get("required_documents") or []),
        )
        field_source_references = _canonical_source_references(
            field.get("evidence") or [],
            canonical_units,
        )
        value = field.get("value")
        atomic_values = _requirement_values(key, value) or [title]
        composite = key in _COMPOSITE_REQUIREMENT_FIELDS
        conditions = [str(item) for item in field.get("conditions") or [] if str(item).strip()]
        exceptions = [str(item) for item in field.get("exceptions") or [] if str(item).strip()]
        for index, atomic_value in enumerate(atomic_values, start=1):
            source_references = (
                [dict(reference) for reference in field_source_references]
                if composite
                else _source_references_for_atomic_value(
                    field_source_references,
                    atomic_value,
                )
            )
            source_quote = next(
                (
                    str(reference.get("quote") or "").strip()
                    for reference in source_references
                    if str(reference.get("quote") or "").strip()
                ),
                None,
            )
            atomic_conditions = _context_for_atomic_value(
                conditions,
                atomic_value,
                len(atomic_values),
            )
            atomic_exceptions = _context_for_atomic_value(
                exceptions,
                atomic_value,
                len(atomic_values),
                shared_relief=True,
            )
            requirement = title if atomic_value == title else f"{title} — {atomic_value}"
            pass_rule = "all_required_evidence_must_pass"
            criterion_evidence = _evidence_for_atomic_value(
                key,
                evidence,
                atomic_value,
                composite=composite or len(atomic_values) == 1,
                source_references=source_references,
            )
            # Buyer specifications define the comparison target; they are not
            # a second artifact that the bidder must submit.
            for evidence_item in criterion_evidence:
                if "buyer specification document" in str(evidence_item.get("document_name") or "").casefold():
                    evidence_item["required"] = False
                    evidence_item["missing_effect"] = "optional"
            if key in {"allowed_legal_entities", "permitted_participant_structure"}:
                pass_rule = (
                    "pass_when_bidder_matches_one_permitted_legal_form_and_no_"
                    "prohibited_participation_structure_is_present"
                )
            if key == "joint_venture_allowed" and value is False:
                requirement = (
                    "JV / consortium participation is prohibited; the bid must be "
                    "submitted by one eligible legal entity."
                )
                pass_rule = (
                    "pass_when_bidder_is_one_eligible_legal_entity_and_no_jv_or_"
                    "consortium_participation_is_present"
                )
                if not criterion_evidence:
                    criterion_evidence.append(
                        {
                            "evidence_id": f"{key}-E1",
                            "document_name": "Bidder legal identity and participation structure",
                            "document_type": "gst_pan_cin_statutory",
                            "required": True,
                            "checks": ["single_legal_entity", "no_jv_or_consortium_participation"],
                            "acceptance_rule": (
                                "The submitted bid identifies one eligible legal entity "
                                "and contains no JV or consortium participation."
                            ),
                            "missing_effect": "manual_review",
                        }
                    )
            if atomic_conditions:
                requirement += ". Conditions: " + "; ".join(atomic_conditions)
            if atomic_exceptions:
                requirement += ". Exceptions: " + "; ".join(atomic_exceptions)
            semantics = _requirement_semantics(
                key,
                atomic_value,
                field_status=(
                    "ambiguous"
                    if str(field.get("status") or "found") == "ambiguous"
                    and _atomic_value_has_source_table_ambiguity(atomic_value, field)
                    else (
                        "found"
                        if str(field.get("status") or "found") == "ambiguous"
                        and (field.get("audit") or {}).get("source_table_ambiguities")
                        else str(field.get("status") or "found")
                    )
                ),
                conditions=atomic_conditions,
                exceptions=atomic_exceptions,
                source_text=" ".join(
                    str(reference.get("quote") or "")
                    for reference in source_references
                ),
                source_grounded=bool(source_references),
            )
            # Preserve the established FIELD-<key> identifier for the first item;
            # only additional atomic checks need semantic, wording-independent suffixes.
            suffix = _criterion_suffix(key, atomic_value, index)
            criterion_id = f"FIELD-{key}{suffix}"
            if criterion_id in criterion_ids:
                criterion_id = f"{criterion_id}-{index}"
            criterion_ids.add(criterion_id)
            if not criterion_evidence and semantics["included_by_default"]:
                criterion_evidence = [_default_evidence_for_atomic_value(key, atomic_value)]
            for evidence_index, evidence_item in enumerate(criterion_evidence, start=1):
                evidence_item["evidence_id"] = f"{criterion_id}-E{evidence_index}"
            evidence_groups = _evidence_groups(
                key,
                atomic_value,
                pass_rule,
                criterion_evidence,
            )
            items.append({
                "criterion_id": criterion_id,
                "field_key": key,
                "requirement": requirement,
                "category": category,
                "stage": (
                    "bid_submission"
                    if semantics["evaluation_scope"] == "technical_qualification"
                    else semantics["evaluation_scope"]
                ),
                "mandatory": (
                    semantics["materiality"] == "mandatory_blocking"
                    and semantics["included_by_default"]
                ),
                "clarifiable": False,
                "failure_effect": (
                    "contract_remedy"
                    if semantics["materiality"] == "post_award"
                    else (
                        "reject_unresponsive"
                        if semantics["materiality"] == "mandatory_blocking"
                        and semantics["included_by_default"]
                        else "manual_review"
                    )
                ),
                "pass_rule": pass_rule,
                "source_quote": source_quote,
                "source_references": source_references,
                "required_evidence": criterion_evidence,
                "extensions": {
                    "provenance": {
                        "source_references": source_references,
                        "status": "grounded" if source_references else "missing",
                        "source_reference_count": len(source_references),
                    },
                    "reviewed_field": {
                        "field_key": key,
                        "atomic_value": atomic_value,
                        "conditions": atomic_conditions,
                        "exceptions": atomic_exceptions,
                        "required_documents": [
                            str(item.get("document_name") or "")
                            for item in criterion_evidence
                            if str(item.get("document_name") or "").strip()
                        ],
                    },
                    "evaluation": {
                        **semantics,
                        "semantic_key": _requirement_semantic_key(key, atomic_value),
                        "evidence_groups": evidence_groups,
                    },
                },
            })
    matrix = {
        "items": items,
        "planning_notes": [
            "Generated only from the reviewed configurable field ledger; unsupported fields remain human-review items."
        ],
    }
    from procurement.bhel_evaluation import merge_bhel_profile

    merged = merge_bhel_profile(matrix, understanding, canonical_units)
    grounded = _attach_matrix_provenance(merged, canonical_units)
    from procurement.evaluation.contracts import normalize_contract

    grounded["items"] = normalize_contract(grounded.get("items") or [])
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    seen_ids: set[str] = set()
    for item in grounded.get("items") or []:
        criterion_id = str(item.get("criterion_id") or "")
        if not criterion_id or criterion_id in seen_ids:
            validation_errors.append(f"Duplicate or empty criterion_id: {criterion_id or '<empty>'}")
        seen_ids.add(criterion_id)
        evidence_ids = {
            str(evidence_item.get("evidence_id") or "")
            for evidence_item in item.get("required_evidence") or []
        }
        evaluation = ((item.get("extensions") or {}).get("evaluation") or {})
        grouped_ids = {
            str(evidence_id)
            for group in evaluation.get("evidence_groups") or []
            for evidence_id in group
        }
        unknown_ids = sorted(grouped_ids - evidence_ids)
        if unknown_ids:
            validation_errors.append(
                f"{criterion_id} references unknown evidence ids: {', '.join(unknown_ids)}"
            )
        if evaluation.get("applicability") == "conditional" and not evaluation.get("applicability_rule"):
            validation_errors.append(f"{criterion_id} has no applicability rule")
        if evaluation.get("included_by_default") and not (item.get("source_references") or []):
            validation_warnings.append(f"{criterion_id} has no grounded tender citation")
    if validation_errors:
        raise ValueError("Invalid generated requirement matrix: " + "; ".join(validation_errors))
    grounded["validation"] = {
        "status": "valid" if not validation_warnings else "valid_with_warnings",
        "errors": [],
        "warnings": validation_warnings,
    }
    return grounded
