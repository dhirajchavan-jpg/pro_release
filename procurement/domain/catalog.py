from typing import get_args

from rfp_atoms import AtomType


PROCUREMENT_SOURCES = [
    {
        "name": "DoE Manual for Procurement of Goods, Second Edition 2024",
        "url": "https://doe.gov.in/files/circulars_document/Manual_Goods_2024.pdf",
        "rule": "Check responsiveness first; required EMD, eligibility, authority, bid validity, and stipulated documents can make a bid unresponsive.",
    },
    {
        "name": "General Financial Rules 2017, updated to 31 January 2026",
        "url": "https://doe.gov.in/files/circulars_document/GFRupdatedupto31012026.pdf",
        "rule": "Evaluation must follow declared criteria; GeM is mandatory for available common-use goods/services; EMD and debarment rules apply.",
    },
    {
        "name": "DoE Manual for Procurement of Works, Second Edition 2025",
        "url": "https://doe.gov.in/files/circulars_document/Works_Manual_SE_2025.pdf",
        "rule": "Use preliminary examination, responsiveness, clarifications, eligibility, techno-commercial evaluation, then financial evaluation.",
    },
    {
        "name": "DoE Manual for Procurement of Non-Consultancy Services 2025",
        "url": "https://doe.gov.in/files/circulars_document/MfPoNCS_2025.pdf",
        "rule": "Evaluation follows the RFP and separates responsiveness/technical evaluation from financial opening.",
    },
    {
        "name": "CPPP Model Tender Document for Goods",
        "url": "https://eprocure.gov.in/cppp/sites/default/files/standard_biddingdocs/MTD%20Goods%20NIC.pdf",
        "rule": "Substantive deviations make bids non-responsive; e-reverse auction runs only when the tender/platform says so.",
    },
    {
        "name": "GeM General Terms and Conditions",
        "url": "https://admin.gem.gov.in/apis/v1/gtc/pdfByDate/?date=20260408",
        "rule": "GeM bids may include GTC, STC, SLA, buyer-added ATC, EMD exemptions, preference policy, and reverse-auction rules.",
    },
    {
        "name": "DoE debarment amendment, 8 May 2026",
        "url": "https://doe.gov.in/files/circulars_document/OM_262026i_08052026.pdf",
        "rule": "Debarment and code-of-integrity findings can make bidders ineligible; bidders must receive reasonable opportunity.",
    },
    {
        "name": "MSME public procurement FAQ",
        "url": "https://www.dcmsme.gov.in/FAQs-PPP_25032022.pdf",
        "rule": "Eligible MSEs may receive tender/EMD benefits and L1+15 purchase-preference treatment when the tender allows policy application.",
    },
    {
        "name": "DPIIT Make in India order, 19 July 2024",
        "url": "https://www.dpiit.gov.in/static/uploads/2025/07/0ee62cb7cfc6586c69e0acd4febb3f2f.pdf",
        "rule": "Class-I/Class-II/non-local supplier status and local-content purchase preference must be evidence-backed.",
    },
]

DOC_TYPES = [
    "tender_notice",
    "rfp_or_bid_document",
    "gem_bid",
    "corrigendum",
    "atc_stc_sla_gtc",
    "technical_specs_or_sow",
    "eligibility_pqc",
    "boq_or_price_schedule",
    "forms_annexures",
    "prebid_clarification",
    "contract_terms",
    "bid_covering_letter",
    "technical_proposal",
    "financial_bid",
    "bid_submission",
    "emd_or_bid_security",
    "bid_security_declaration",
    "bank_guarantee",
    "mse_udyam_or_startup_exemption",
    "mii_local_content",
    "land_border_declaration",
    "oem_authorization",
    "poa_or_board_authorization",
    "gst_pan_cin_statutory",
    "gem_registration",
    "registration_document",
    "statutory_license",
    "environmental_statutory",
    "labour_compliance",
    "turnover_networth_ca_certificate",
    "audited_financials_or_itr",
    "bank_solvency",
    "experience_po_completion",
    "past_work_evidence",
    "quality_certificate",
    "calibration_certificate",
    "datasheet_catalogue",
    "integrity_pact",
    "integrity_blacklisting_declaration",
    "non_blacklisting_declaration",
    "nil_deviation_declaration",
    "tender_acceptance",
    "techno_commercial_compliance",
    "consortium_jv",
    "commercial_or_price",
    "other",
    "unknown",
]

VENDOR_CLASSIFICATION_RULES = [
    "Classify from document content, not filename alone.",
    "Use unknown when the evidence is insufficient or genuinely ambiguous; never guess.",
    "Udyam/MSME registration proves enterprise status and preference eligibility; do not call it bid security merely because an EMD exemption might be claimed.",
    "Use bid_security_declaration only for the prescribed declaration replacing EMD when the tender permits it; use emd_or_bid_security for payment, BG, or instrument proof.",
    "Keep land_border_declaration separate from mii_local_content: Rule 144(xi)/country-sharing-land-border language is not a local-content certificate.",
    "Use eligibility_pqc for a bidder's point-wise PQR/pre-qualification response; keep it separate from experience_po_completion invoices, POs, and completion proofs.",
    "Keep integrity_pact, non_blacklisting_declaration, nil_deviation_declaration, and tender_acceptance distinct when the content supports that distinction.",
    "Use techno_commercial_compliance for unpriced confirmations of delivery, validity, payment, quantity tolerance, taxes, freight, insurance, penalty, arbitration, force majeure, or other tender terms.",
    "Never classify an unpriced techno-commercial compliance statement as price material.",
    "Any BOQ, quoted rate, price schedule, financial bid, or commercial price material must be commercial_or_price or financial_bid.",
    "For tender uploads, source tender/RFP/GeM/ATC/GPC material is tender_source.",
    "For vendor uploads, signed/stamped/filled tender forms, declarations, and compliance sheets are vendor_submission evidence.",
    "For vendor uploads, unsigned duplicate tender/RFP source material without bidder acceptance/signature is reference, not evidence.",
]

BASE_RFP_DOCUMENT_TYPES = {
    "tender_notice",
    "rfp_or_bid_document",
    "gem_bid",
    "atc_stc_sla_gtc",
    "technical_specs_or_sow",
    "eligibility_pqc",
    "boq_or_price_schedule",
    "forms_annexures",
    "contract_terms",
}
POST_BASE_RFP_DOCUMENT_TYPES = {"corrigendum", "prebid_clarification"}
ATOM_TYPES = tuple(get_args(AtomType))
GROUPABLE_ATOM_TYPES = ("clause", "other", "definition", "non_semantic")
