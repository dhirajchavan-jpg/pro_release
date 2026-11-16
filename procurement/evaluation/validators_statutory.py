"""Statutory identifier and proof-contract validators."""

from __future__ import annotations

import re
from typing import Any

from procurement.domain.schemas import CriterionValidatorResult, RequirementMatrixItem
from procurement.domain.text import humanize_check_name, keyword_tokens, normalize_for_match, unique_items
from procurement.evaluation.criteria import criterion_atomic_requirement_text
from procurement.evaluation.evidence import (
    EntityRole as EvidenceEntityRole,
    EvidenceLedger as EntityEvidenceLedger,
    EvidenceObservation,
    IdentifierType,
    ProofContract as EntityProofContract,
    ProofStrength,
    ReconciliationStatus,
)
from procurement.evaluation.facts import identifier_matches_by_role
from .validators_shared import validator_result


def validate_statutory_identifier_criterion(
    item: RequirementMatrixItem,
    text: str,
    candidate: dict[str, Any],
) -> CriterionValidatorResult:
    requirement = criterion_atomic_requirement_text(item)
    requirement_tokens = keyword_tokens(requirement)
    checks: dict[str, str] = {}
    needs_pan = "pan" in requirement_tokens
    needs_gstin = bool(requirement_tokens.intersection({"gst", "gstin"}))
    needs_cin = "cin" in requirement_tokens
    entity_ledger = candidate.get("bidder_identifier_ledger")
    if isinstance(entity_ledger, EntityEvidenceLedger):
        return validate_statutory_identifier_from_ledger(
            item,
            candidate,
            entity_ledger,
            needs_pan=needs_pan,
            needs_gstin=needs_gstin,
            needs_cin=needs_cin,
        )
    pan_by_role = identifier_matches_by_role(text, r"\b[a-z]{5}\d{4}[a-z]\b")
    gstin_by_role = identifier_matches_by_role(
        text,
        r"\b\d{2}[a-z]{5}\d{4}[a-z][a-z0-9]z[a-z0-9]\b",
    )
    gstin_match = next(iter(gstin_by_role.get("bidder") or []), None)
    if gstin_match is None:
        unknown_gstins = list(gstin_by_role.get("unknown") or [])
        if len(unknown_gstins) == 1 and any(
            marker in text
            for marker in (
                "gst registration",
                "goods and services tax",
                "registration certificate",
                "gstin",
            )
        ):
            gstin_match = unknown_gstins[0]
    gstin_value = gstin_match.group(0).upper() if gstin_match else ""
    pan_match = next(iter(pan_by_role.get("bidder") or []), None)
    pan_value = pan_match.group(0).upper() if pan_match else ""
    if not pan_value and gstin_value:
        pan_value = gstin_value[2:12]
    if not pan_value:
        pan_document_candidates = list(pan_by_role.get("unknown") or []) + list(
            pan_by_role.get("issuer") or []
        )
        if len(pan_document_candidates) == 1 and any(
            marker in text
            for marker in ("permanent account number", "pan card", "income tax department")
        ):
            pan_value = pan_document_candidates[0].group(0).upper()
    cin_match = re.search(r"\b[ul]\d{5}[a-z]{2}\d{4}[a-z]{3}\d{6}\b", text)
    if needs_pan:
        checks["pan"] = "pass" if pan_value else "missing"
    if needs_gstin:
        checks["gstin"] = "pass" if gstin_match else "missing"
    if needs_cin:
        checks["cin"] = "pass" if cin_match else "missing"
    if not checks:
        has_any_identifier = bool(
            re.search(r"(?:\bpan\s*[:\-]?\s*)?([a-z]{5}\d{4}[a-z])\b", text)
            or re.search(r"\b\d{2}[a-z]{5}\d{4}[a-z][a-z0-9]z[a-z0-9]\b", text)
            or re.search(r"\b[ul]\d{5}[a-z]{2}\d{4}[a-z]{3}\d{6}\b", text)
        )
        if has_any_identifier:
            return validator_result(
                item,
                "pass",
                "Statutory identifier evidence is present.",
                {"statutory_identifier": "pass"},
                0.82,
                False,
                candidate,
            )
        return validator_result(
            item,
            "unchanged",
            "No statutory identifier check applies.",
            {},
            0.0,
            False,
            candidate,
        )
    if all(value == "pass" for value in checks.values()):
        if pan_value and gstin_value and gstin_value[2:12] != pan_value:
            return validator_result(
                item,
                "fail",
                "Bidder PAN does not match the PAN embedded in bidder GSTIN.",
                {**checks, "gstin_pan_reconciliation": "fail"},
                0.98,
                True,
                candidate,
            )
        return validator_result(
            item,
            "pass",
            "Required statutory identifier evidence is present.",
            checks,
            0.9,
            False,
            candidate,
        )
    missing = [
        humanize_check_name(key)
        for key, value in checks.items()
        if value == "missing"
    ]
    found = [
        f"PAN: {pan_value}" if pan_value else "",
        f"GSTIN: {gstin_value}" if gstin_value else "",
        f"CIN: {cin_match.group(0).upper()}" if cin_match else "",
    ]
    objective = "; ".join(
        [value for value in found if value]
        + [f"{name.upper()}: missing" for name in missing]
    )
    return validator_result(
        item,
        "missing",
        objective,
        checks,
        0.85,
        False,
        candidate,
        missing_documents=[f"{name.upper()} evidence" for name in missing],
    )

def validate_statutory_identifier_from_ledger(
    item: RequirementMatrixItem,
    candidate: dict[str, Any],
    ledger: EntityEvidenceLedger,
    *,
    needs_pan: bool,
    needs_gstin: bool,
    needs_cin: bool,
) -> CriterionValidatorResult:
    requested = [
        identifier_type
        for needed, identifier_type in (
            (needs_pan, IdentifierType.PAN),
            (needs_gstin, IdentifierType.GSTIN),
        )
        if needed
    ]
    if needs_cin and not requested:
        return validator_result(
            item,
            "unchanged",
            "CIN is not yet represented in the typed bidder identifier ledger.",
            {},
            0.0,
            False,
            candidate,
        )
    checks: dict[str, str] = {}
    reasons: list[str] = []
    missing_documents: list[str] = []
    source_observations: list[EvidenceObservation] = []
    hard_failure = False
    proof_contract_unsatisfied = False
    for identifier_type in requested:
        key = identifier_type.value
        exact_artifact = identifier_artifact_required(item, identifier_type)
        observations = ledger.satisfying(
            EntityProofContract(
                entity_role=EvidenceEntityRole.BIDDER,
                entity_id="bidder",
                identifier_type=identifier_type,
            )
        )
        values = sorted(
            {observation.normalized_value or observation.value for observation in observations}
        )
        source_observations.extend(observations)
        if not values:
            checks[key] = "missing"
            reasons.append(f"Bidder {key.upper()}: missing")
            missing_documents.extend(required_identifier_documents(item, identifier_type))
            proof_contract_unsatisfied = proof_contract_unsatisfied or exact_artifact
            continue
        if len(values) > 1:
            if identifier_type is IdentifierType.GSTIN and len({value[2:12] for value in values}) == 1:
                checks[key] = "unclear"
                reasons.append("Multiple bidder GSTINs share the same PAN; verify which registration satisfies this requirement: " + ", ".join(values))
                continue
            checks[key] = "fail"
            reasons.append(f"Conflicting bidder {key.upper()} values: {', '.join(values)}")
            hard_failure = True
            continue
        value = values[0]
        checks[key] = "pass"
        accepted_labels = {
            "udyam_certificate",
            "pan_card" if identifier_type is IdentifierType.PAN else "gst_registration",
        }
        if identifier_type is IdentifierType.GSTIN:
            accepted_labels.add("gst_registration")
        proof_observations = ledger.satisfying(
            EntityProofContract(
                minimum_strength=ProofStrength.ISSUER_CERTIFICATE,
                accepted_artifact_labels=accepted_labels,
                required_capabilities={"statutory_identifier"},
                entity_role=EvidenceEntityRole.BIDDER,
                entity_id="bidder",
                identifier_type=identifier_type,
                issuer_role=EvidenceEntityRole.ISSUER,
            )
        )
        source = min(
            observations,
            key=lambda observation: (
                observation.provenance.document_id,
                observation.provenance.page_number,
            ),
        )
        metadata = dict(source.metadata)
        source_text = (
            f"{metadata.get('file_name', source.provenance.document_id)} "
            f"p.{source.provenance.page_number}"
        )
        if exact_artifact and not proof_observations:
            checks[f"{key}_artifact"] = "missing"
            proof_contract_unsatisfied = True
            required = required_identifier_documents(item, identifier_type)
            missing_documents.extend(required)
            reasons.append(
                f"Bidder {key.upper()} {value} was found on {source_text}, but the required "
                f"{', '.join(required) or key.upper() + ' registration artifact'} was not found."
            )
        else:
            checks[f"{key}_artifact"] = "pass"
            reasons.append(f"Bidder {key.upper()} {value} verified from {source_text}.")

    reconciliation = ledger.reconcile_gstin_to_bidder_pan("bidder")
    if reconciliation.status in {
        ReconciliationStatus.MISMATCH,
        ReconciliationStatus.CONFLICTING_GSTIN,
        ReconciliationStatus.CONFLICTING_BIDDER_PAN,
    }:
        checks["gstin_pan_reconciliation"] = "fail"
        reasons.append("Bidder PAN and GSTIN are inconsistent across submitted evidence.")
        hard_failure = True
    elif reconciliation.status is ReconciliationStatus.MATCHED:
        checks["gstin_pan_reconciliation"] = "pass"

    # A generic model finding cannot satisfy an exact-artifact contract with an
    # incidental identifier printed on an invoice or third-party document.
    hard_failure = hard_failure or proof_contract_unsatisfied

    enriched_candidate = dict(candidate)
    enriched_candidate["documents"] = sorted(
        {
            *candidate.get("documents", []),
            *(
                dict(observation.metadata).get("file_name", "")
                for observation in source_observations
            ),
        }
        - {""}
    )
    enriched_candidate["document_ids"] = sorted(
        {
            *candidate.get("document_ids", []),
            *(observation.provenance.document_id for observation in source_observations),
        }
    )
    enriched_candidate["page_ids"] = sorted(
        {
            *candidate.get("page_ids", []),
            *(
                observation.provenance.page_id
                for observation in source_observations
                if observation.provenance.page_id
            ),
        }
    )
    if any(value == "fail" for value in checks.values()):
        status = "fail"
    elif any(value == "missing" for value in checks.values()):
        status = "missing"
    elif any(value == "unclear" for value in checks.values()):
        status = "unclear"
    else:
        status = "pass"
        checks["entity_scoped_identifier_proof"] = "pass"
    return validator_result(
        item,
        status,
        " ".join(reasons),
        checks,
        0.98 if hard_failure else 0.96,
        hard_failure,
        enriched_candidate,
        missing_documents=unique_items(missing_documents),
    )

def identifier_artifact_required(
    item: RequirementMatrixItem,
    identifier_type: IdentifierType,
) -> bool:
    text = normalize_for_match(
        " ".join(
            f"{evidence.document_name} {evidence.acceptance_rule}"
            for evidence in item.required_evidence
            if evidence.required
        )
    )
    if identifier_type is IdentifierType.PAN:
        return any(marker in text for marker in ("pan card", "copy of pan"))
    return any(
        marker in text
        for marker in ("gst registration", "copy of gstin", "gstin certificate")
    )

def required_identifier_documents(
    item: RequirementMatrixItem,
    identifier_type: IdentifierType,
) -> list[str]:
    marker = "pan" if identifier_type is IdentifierType.PAN else "gst"
    names = [
        evidence.document_name
        for evidence in item.required_evidence
        if evidence.required and marker in normalize_for_match(evidence.document_name)
    ]
    return names or [f"{identifier_type.value.upper()} evidence"]

__all__ = [
    "identifier_artifact_required",
    "required_identifier_documents",
    "validate_statutory_identifier_criterion",
    "validate_statutory_identifier_from_ledger",
]
