"""Deterministic bidder fact extraction used by evaluation rules."""

from __future__ import annotations

import re

from procurement.domain.schemas import ExtractedDocument
from procurement.domain.text import clean_text, normalize_for_match, unique_items
from procurement.evaluation.evidence import (
    EntityRole as EvidenceEntityRole,
    EvidenceLedger as EntityEvidenceLedger,
    EvidenceObservation,
    IdentifierType,
    ProofStrength,
    SourceProvenance,
)


def normalize_fact_value(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def identifier_entity_role(text: str, start: int, end: int) -> str:
    """Classify an identifier from its nearest local label."""
    before = text[max(0, start - 180):start]
    markers = {
        "bidder": (
            "bidder", "vendor", "supplier", "seller", "consignor", "enterprise",
            "company pan", "company's pan", "company s pan",
            "company gst", "company's gst", "company s gst",
            "company gstin", "company's gstin", "company s gstin",
            "applicant", "legal name", "owner pan",
        ),
        "director": ("director", "promoter", "partner pan", "shareholder", "proprietor"),
        "customer": (
            "customer", "client", "consignee", "buyer gst", "buyer pan", "buyer",
            "purchaser", "bill to", "billed to", "ship to", "shipped to", "recipient",
            "receiver",
        ),
        "related_party": (
            "parent company", "subsidiary", "related party", "group company",
            "associate company", "unsecured loan", "amount due to", "amount due from",
        ),
        "issuer": ("issuer", "issuing authority", "certifying authority", "income tax department"),
    }
    ranked: list[tuple[int, str]] = []
    for role, values in markers.items():
        for marker in values:
            position = before.rfind(marker)
            if position >= 0:
                ranked.append((len(before) - position - len(marker), role))
    return min(ranked)[1] if ranked else "unknown"


def identifier_matches_by_role(text: str, pattern: str) -> dict[str, list[re.Match[str]]]:
    matches: dict[str, list[re.Match[str]]] = {}
    for match in re.finditer(pattern, text):
        matches.setdefault(identifier_entity_role(text, match.start(), match.end()), []).append(match)
    return matches


def clean_party_name(value: str) -> str:
    text = clean_text(value).upper()
    text = re.split(r"\b(?:BUYER|PO DATE|DATED|DATE|VALUE|AMOUNT|ADDRESS|GSTIN|PAN|INVOICE|ORDER)\b", text)[0]
    text = re.sub(r"[^A-Z0-9 &()./-]+", " ", text).strip(" :-")
    return text if len(text) >= 4 else ""


def unfilled_local_content_template(text: str) -> bool:
    raw = str(text or "")
    normalized = normalize_for_match(raw)
    blank_percent = bool(
        re.search(r"local content\s+(?:of|is)\s*(?:_+|\.{3,}|\[\s*\])\s*%", raw, re.IGNORECASE)
    )
    explicit_percent = bool(
        re.search(r"local content\s+(?:of|is)\s*\d+(?:\.\d+)?\s*%", normalized)
    )
    return blank_percent and not explicit_percent


def declared_local_content_percent(text: str) -> str | None:
    normalized = normalize_for_match(text)
    if match := re.search(
        r"local content\s+(?:of|is|percentage\s*(?:is|:))\s*(\d+(?:\.\d+)?)\s*%",
        normalized,
    ):
        return match.group(1)
    if any(marker in normalized for marker in ("we hereby certify", "we certify", "hereby declare")):
        if match := re.search(r"(\d+(?:\.\d+)?)\s*%.{0,80}local content", normalized):
            return match.group(1)
    return None


def extract_packet_facts(text: str, *, document_type: str = "") -> dict[str, str]:
    facts: dict[str, str] = {}
    normalized = normalize_for_match(text)
    if match := re.search(r"\budyam-[a-z]{2}-\d{2}-\d{7}\b", normalized):
        facts["udyam_number"] = match.group(0).upper()
    if match := re.search(
        r"\b(?:enterprise name|name of enterprise|legal name|company name|applicant name|"
        r"seller name|supplier name|vendor name|contractor name|bidder name)"
        r"\s*[:.-]\s*([a-z0-9 &()./-]{4,80})",
        normalized,
    ):
        party_name = clean_party_name(match.group(1))
        if party_name:
            facts["party_name"] = party_name
            facts["party_name_entity_role"] = "bidder"
    if "party_name" not in facts:
        if match := re.search(
            r"this is to certify that\s+([a-z0-9 &()./-]{4,80}?)\s+(?:completed|has|supplied|executed|undertaken|performed)",
            normalized,
        ):
            party_name = clean_party_name(match.group(1))
            if party_name:
                facts["party_name"] = party_name
                facts["party_name_entity_role"] = "unknown"
    if match := re.search(r"\b\d{2}\d{6}[a-z0-9]{10}\b", normalized):
        facts["udin"] = match.group(0).upper()
    gstin_matches = identifier_matches_by_role(
        normalized,
        r"\b\d{2}[a-z]{5}\d{4}[a-z][a-z0-9]z[a-z0-9]\b",
    )
    gstin_candidates = list(gstin_matches.get("bidder") or [])
    gstin_role = "bidder"
    if not gstin_candidates:
        unknown_gstins = list(gstin_matches.get("unknown") or [])
        registration_context = any(
            marker in normalized
            for marker in ("gst registration", "goods and services tax registration", "registration certificate")
        )
        explicit_gstin_label = bool(
            re.search(r"\bgstin(?:\s*/\s*uin)?\s*(?:number|no\.?)?\s*[:.-]", normalized)
        )
        if len(unknown_gstins) == 1 and (
            registration_context
            or (document_type == "gst_pan_cin_statutory" and explicit_gstin_label)
        ):
            gstin_candidates = unknown_gstins
            gstin_role = "bidder"
    if gstin_candidates:
        match = gstin_candidates[0]
        facts["gstin"] = match.group(0).upper()
        facts["gstin_entity_role"] = gstin_role
        facts["gstin_confidence"] = "high"
    pan_matches = identifier_matches_by_role(normalized, r"\b[a-z]{5}\d{4}[a-z]\b")
    pan_candidates = list(pan_matches.get("bidder") or [])
    if pan_candidates:
        match = pan_candidates[0]
        facts["pan"] = match.group(0).upper()
        facts["pan_entity_role"] = "bidder"
        facts["pan_confidence"] = "high"
    elif facts.get("gstin") and len(facts["gstin"]) == 15:
        facts["pan"] = facts["gstin"][2:12]
        facts["pan_entity_role"] = "bidder"
        facts["pan_confidence"] = "high"
        facts["pan_source"] = "derived_from_bidder_gstin"
    else:
        unknown_pans = list(pan_matches.get("unknown") or []) + list(pan_matches.get("issuer") or [])
        pan_document_context = any(
            marker in normalized
            for marker in ("permanent account number", "pan card", "income tax department")
        )
        explicit_pan_label = bool(
            re.search(r"\bpan(?:\s*/\s*it)?\s*(?:number|no\.?)?\s*[:.-]", normalized)
        )
        if len(unknown_pans) == 1 and (
            pan_document_context
            or (document_type == "gst_pan_cin_statutory" and explicit_pan_label)
        ):
            facts["pan"] = unknown_pans[0].group(0).upper()
            facts["pan_entity_role"] = "bidder"
            facts["pan_confidence"] = "high"
    # Multi-party invoices commonly print seller and buyer GSTINs together.
    # Once bidder PAN is known, the GSTIN embedding that same PAN is the
    # deterministic entity link; nearby table labels alone are not reliable.
    if not facts.get("gstin") and facts.get("pan"):
        eligible_gstins = [
            match
            for role, matches in gstin_matches.items()
            if role not in {"customer", "director", "related_party", "issuer"}
            for match in matches
            if match.group(0).upper()[2:12] == facts["pan"]
        ]
        if len({match.group(0).upper() for match in eligible_gstins}) == 1:
            facts["gstin"] = eligible_gstins[0].group(0).upper()
            facts["gstin_entity_role"] = "bidder"
            facts["gstin_confidence"] = "high"
    if match := re.search(r"\b(?:po|work order|purchase order|loi|letter of intent)\s*(?:(?:no|number|#|ref|id)\s*[:.-]?|[:.-])\s*([a-z0-9/-]{4,})", normalized):
        facts["po_no"] = match.group(1).upper()
    if match := re.search(r"\binvoice\s*(?:(?:no|number|#|ref|id)\s*[:.-]?|[:.-])\s*([a-z0-9/-]{4,})", normalized):
        facts["invoice_no"] = match.group(1).upper()
    if match := re.search(r"\b(?:utr|transaction)\s*(?:(?:no|number|#|ref|id)\s*[:.-]?|[:.-])\s*([a-z0-9/-]{6,})", normalized):
        facts["utr"] = match.group(1).upper()
    if match := re.search(r"\b(?:bg|bank guarantee)\s*(?:(?:no|number|#|ref|id)\s*[:.-]?|[:.-])\s*([a-z0-9/-]{4,})", normalized):
        facts["bg_no"] = match.group(1).upper()
    blank_local_content = unfilled_local_content_template(text)
    local_content_percent = None if blank_local_content else declared_local_content_percent(normalized)
    if local_content_percent:
        facts["local_content_percent"] = local_content_percent
    amounts = re.findall(r"(?:inr|rs\.?|₹)\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*(?:lakh|lakhs|crore|cr)", normalized)
    if amounts:
        facts["amounts"] = "; ".join(amounts[:5])
    dates = re.findall(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b", normalized)
    if dates:
        facts["dates"] = "; ".join(dates[:5])
    if local_content_percent and ("class-i" in normalized or "class i" in normalized):
        facts["local_supplier_class"] = "Class-I"
    elif local_content_percent and ("class-ii" in normalized or "class ii" in normalized):
        facts["local_supplier_class"] = "Class-II"
    return facts


def canonical_evidence_type(document_type: str) -> str:
    aliases = {
        "experience_po": "experience_po_completion",
        "experience_execution_proof": "experience_po_completion",
        "financial_statement": "turnover_networth_ca_certificate",
        "financial_certificate": "turnover_networth_ca_certificate",
        "bank_guarantee": "emd_or_bid_security",
        "financial_instrument": "emd_or_bid_security",
        "self_declaration": "forms_annexures",
        "bid_security_declaration": "emd_or_bid_security",
        "land_border_declaration": "forms_annexures",
        "integrity_pact": "integrity_blacklisting_declaration",
        "non_blacklisting_declaration": "integrity_blacklisting_declaration",
        "nil_deviation_declaration": "forms_annexures",
        "tender_acceptance": "forms_annexures",
        "commercial_or_price": "financial_bid",
        "calibration_record": "calibration_certificate",
        "calibration_records": "calibration_certificate",
    }
    return aliases.get(document_type, document_type)


def exact_product_identity_anchors(text: str) -> list[str]:
    """Return exact mixed letter/number identifiers from an offered item description."""
    normalized = normalize_for_match(text)
    return unique_items(
        re.findall(r"\b(?=[a-z0-9-]{5,}\b)(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+\b", normalized)
    )


def bidder_identifier_evidence_ledger(docs: list[ExtractedDocument]) -> EntityEvidenceLedger:
    """Build one bidder-scoped identifier ledger reused by every criterion."""
    observations: list[EvidenceObservation] = []
    for document in docs:
        if document.record.upload_side != "vendor":
            continue
        for page in document.pages:
            document_type = (
                page.classification.document_type
                if page.classification
                else document.record.classification.document_type
                if document.record.classification
                else "unknown"
            )
            facts = extract_packet_facts(page.text, document_type=document_type)
            for fact_key, identifier_type in (
                ("pan", IdentifierType.PAN),
                ("gstin", IdentifierType.GSTIN),
            ):
                value = str(facts.get(fact_key) or "").strip()
                if not value or facts.get(f"{fact_key}_entity_role") != "bidder":
                    continue
                labels, capabilities = strict_identifier_artifact_metadata(
                    identifier_type,
                    page.text,
                    document_type,
                )
                proof_strength = (
                    ProofStrength.ISSUER_CERTIFICATE
                    if labels
                    else ProofStrength.CLAIM
                )
                try:
                    observations.append(
                        EvidenceObservation(
                            observation_id=f"{page.page_id}:{fact_key}:{value}",
                            fact_type=fact_key,
                            value=value,
                            entity_id="bidder",
                            entity_role=EvidenceEntityRole.BIDDER,
                            provenance=SourceProvenance(
                                document_id=document.record.document_id,
                                page_number=page.page_number,
                                page_id=page.page_id,
                                quote=identifier_source_quote(page.text, value),
                            ),
                            proof_strength=proof_strength,
                            artifact_labels=frozenset(labels),
                            capabilities=frozenset(capabilities),
                            identifier_type=identifier_type,
                            issuer_role=(
                                EvidenceEntityRole.ISSUER
                                if proof_strength is ProofStrength.ISSUER_CERTIFICATE
                                else None
                            ),
                            metadata={
                                "file_name": document.record.file_name,
                                "document_type": document_type,
                            },
                        )
                    )
                except ValueError:
                    continue
    unique = {observation.observation_id: observation for observation in observations}
    return EntityEvidenceLedger(tuple(unique.values()))


def strict_identifier_artifact_metadata(
    identifier_type: IdentifierType,
    text: str,
    document_type: str,
) -> tuple[set[str], set[str]]:
    """Recognize an actual registration artifact, not an incidental number."""
    normalized = normalize_for_match(text[:12000])
    document_key = normalize_for_match(document_type).replace(" ", "_")
    if any(marker in normalized for marker in ("tax invoice", "invoice no", "e invoice")):
        return set(), set()
    is_udyam = (
        document_key in {"udyam_certificate", "mse_udyam_or_startup_exemption"}
        or "udyam registration" in normalized
    )
    if is_udyam:
        return ({"udyam_certificate"}, {"bidder_identity", "statutory_identifier", "mse_status"})
    if identifier_type is IdentifierType.PAN:
        exact = (
            document_key == "pan_card"
            or "permanent account number card" in normalized
            or ("income tax department" in normalized and "permanent account number" in normalized)
        )
        return ({"pan_card"}, {"bidder_identity", "statutory_identifier"}) if exact else (set(), set())
    exact = (
        document_key == "gst_registration"
        or "form gst reg-06" in normalized
        or (
            "registration certificate" in normalized
            and any(marker in normalized for marker in ("goods and services tax", "gstin"))
        )
    )
    return ({"gst_registration"}, {"bidder_identity", "statutory_identifier"}) if exact else (set(), set())


def identifier_source_quote(text: str, value: str) -> str:
    compact = clean_text(text)
    index = compact.upper().find(value.upper())
    if index < 0:
        return value
    return compact[max(0, index - 100):min(len(compact), index + len(value) + 100)]


__all__ = [
    "bidder_identifier_evidence_ledger",
    "canonical_evidence_type",
    "clean_party_name",
    "declared_local_content_percent",
    "exact_product_identity_anchors",
    "extract_packet_facts",
    "identifier_entity_role",
    "identifier_matches_by_role",
    "identifier_source_quote",
    "normalize_fact_value",
    "strict_identifier_artifact_metadata",
    "unfilled_local_content_template",
]
