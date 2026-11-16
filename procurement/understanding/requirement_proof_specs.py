"""Tender-named proof specifications for bidder evaluation."""

from __future__ import annotations

import re
from typing import Any

from .requirement_evidence_catalog import _FIELD_EVIDENCE_SPECS, _evidence_match_tokens


def _named_document_spec(document_name: str, field_key: str) -> dict[str, Any] | None:
    name = str(document_name).strip()
    if not name:
        return None
    conditional = any(
        marker in name.casefold()
        for marker in (
            "if bid value",
            "if the bid value",
            "when bid value",
            "where bid value",
            "above rs",
            "exceeds rs",
            "more than rs",
        )
    )
    name_tokens = _evidence_match_tokens(name)
    defaults = list(_FIELD_EVIDENCE_SPECS.get(field_key) or [])
    if defaults:
        scored = [
            (
                len(name_tokens & _evidence_match_tokens(
                    f"{spec.get('document_name', '')} {' '.join(spec.get('checks') or [])}"
                )),
                spec,
            )
            for spec in defaults
        ]
        best = max((score for score, _spec in scored), default=0)
        if best > 0:
            spec = dict(next(spec for score, spec in scored if score == best))
            spec["document_name"] = name
            spec["required"] = bool(spec.get("required", True)) and not conditional
            return spec
    return {
        "document_name": name,
        "document_type": _proof_document_type(field_key, name),
        "checks": (
            ["conditional_applicability", "presence", "tender_requirement_match"]
            if conditional
            else ["presence", "tender_requirement_match"]
        ),
        "required": not conditional,
    }


def _proof_document_type(field_key: str, document_name: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9]+", document_name.casefold()))
    if any(marker in text for marker in ("authorization", "authorisation", "channel partner")):
        return "oem_authorization"
    if any(marker in text for marker in ("datasheet", "data sheet")):
        return "technical_proposal"
    if any(marker in text for marker in ("technical offer", "techno commercial", "techno-commercial")):
        return "technical_proposal"
    if any(marker in text for marker in ("purchase order", "work order", "completion certificate")):
        return "past_work"
    if any(marker in text for marker in ("udyam", "mse", "msme")):
        return "mse_udyam_or_startup_exemption"
    if any(marker in text for marker in ("local content", "make in india")):
        return "mii_local_content"
    if field_key == "regulatory_and_statutory_obligations":
        return _reviewed_document_type(field_key, document_name)
    return "unknown"


def _pqr_named_proofs(atomic_value: str, source_references: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Read the proofs the PQR/requirement actually names instead of a work-order default."""
    blob = " ".join(
        [
            atomic_value,
            *(
                str(reference.get("quote") or reference.get("text") or "")
                for reference in source_references or []
                if isinstance(reference, dict)
            ),
        ]
    ).casefold()
    if not any(
        marker in blob
        for marker in (
            "pre-qualification",
            "pre qualification",
            "annexure-pqr",
            "annexure pqr",
            "pqr",
            "authorization letter",
            "authorisation letter",
            "datasheet",
            "data sheet",
        )
    ):
        return []
    named: list[dict[str, Any]] = []
    for document_name, markers in (
        (
            "OEM / channel-partner authorization letter",
            ("authorization letter", "authorisation letter", "oem authorization", "channel partner"),
        ),
        (
            "Technical datasheet of the offered grade",
            ("technical datasheet", "data sheet", "datasheet"),
        ),
        (
            "Technical offer confirming material, grade, specification, quantity, TC and GC",
            ("technical offer", "techno-commercial", "techno commercial"),
        ),
        (
            "Executed purchase orders or acceptance evidence named by the PQR",
            ("purchase order", "work order", "completion certificate", "acceptance of material"),
        ),
    ):
        if any(marker in blob for marker in markers):
            spec = _named_document_spec(document_name, "similar_project_qualification")
            if spec:
                named.append(spec)
    return named


def evidence_requirements_for_field(
    key: str,
    title: str,
    required_documents: list[Any],
) -> list[dict[str, Any]]:
    named: list[dict[str, Any]] = []
    for document in required_documents:
        spec = _named_document_spec(str(document), key)
        if spec:
            named.append(spec)
    defaults = list(_FIELD_EVIDENCE_SPECS.get(key) or [])
    specs = named or defaults
    if named:
        named_checks = {tuple(spec.get("checks") or []) for spec in named}
        specs = [
            *named,
            *[
                spec
                for spec in defaults
                if not spec.get("required", True)
                and tuple(spec.get("checks") or []) not in named_checks
            ],
        ]
    return [
        {
            "evidence_id": f"{key}-E{index}",
            "document_name": str(spec["document_name"]),
            "document_type": str(spec.get("document_type") or "unknown"),
            "required": bool(spec.get("required", True)),
            "checks": [str(check) for check in spec.get("checks") or []],
            "acceptance_rule": str(
                spec.get("acceptance_rule")
                or f"Evidence must satisfy the reviewed {title.lower()} requirement."
            ),
            "missing_effect": str(spec.get("missing_effect") or "manual_review"),
        }
        for index, spec in enumerate(specs, start=1)
    ]


def _reviewed_document_type(field_key: str, document_name: str) -> str:
    """Retain reviewed statutory documents without turning prose into work history."""
    if field_key != "regulatory_and_statutory_obligations":
        return "unknown"
    text = " ".join(re.findall(r"[a-z0-9]+", document_name.casefold()))
    if "land border" in text:
        return "land_border_declaration"
    if any(marker in text for marker in ("air act", "water act", "pollution", "environment", "consent")):
        return "environmental_statutory"
    if any(marker in text for marker in ("gst", "gstr", "pan", "cin")):
        return "gst_pan_cin_statutory"
    if any(marker in text for marker in ("labour", "epf", "esic", "provident fund")):
        return "labour_compliance"
    return "statutory_license"


__all__ = [
    "_named_document_spec",
    "_pqr_named_proofs",
    "_proof_document_type",
    "_reviewed_document_type",
    "evidence_requirements_for_field",
]

