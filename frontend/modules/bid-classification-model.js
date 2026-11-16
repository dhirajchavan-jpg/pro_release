"use strict";
/* Bidder classification data and display helpers. */

export var BUSINESS_DOCUMENT_CATEGORIES = [
  { value: "company", label: "Bidder Eligibility & Statutory Status" },
  { value: "security", label: "EMD, MSE & Exemptions" },
  { value: "pqr", label: "PQR & Source Approval" },
  { value: "technical", label: "Technical & QAX Scrutiny" },
  { value: "financial", label: "Financial Qualification" },
  { value: "declarations", label: "Declarations & Tender Acceptance" },
  { value: "techno_commercial", label: "Techno-Commercial Terms (Unpriced)" },
  { value: "commercial", label: "Price Bid (Restricted)" },
];

export function manualDocumentLabel(document, units) {
  var fileName = (document.file_name || "").toLowerCase();
  if (/integrity.pact|emd|bid.security/.test(fileName)) return "Integrity pact / bid security";
  if (/technical/.test(fileName)) return "Technical proposal";
  var evidenceText = (units || []).slice(0, 3).map(function (unit) {
    return unit.text_excerpt || "";
  }).join(" ");
  var text = (fileName + " " + evidenceText + " " +
    (document.document_type || "")).toLowerCase();
  if (/blacklist|debar|holiday/.test(text)) return "Non-blacklisting declaration";
  if (/local.content|ppp.mii|make.in.india/.test(text)) return "Local-content declaration";
  if (/\b(gst|gstin|pan|cin|udyam|mse|msme|startup|registration)\b/.test(text)) return "Registration documents";
  if (/work.order|purchase.order|completion|similar.work|letter.of.virtual.completion/.test(text)) return "Past-work evidence";
  if (/integrity.pact|emd|bid.security/.test(text)) return "Integrity pact / bid security";
  if (/turnover|networth|net.worth|audited|financial|itr|ca.certificate/.test(text)) return "Financial certificate";
  if (/quality|iso/.test(text)) return "Quality certificate";
  if (/technical/.test(text)) return "Technical proposal";
  if (/covering.letter/.test(text)) return "Bid covering letter";
  if (/poa|board.authorization|authorisation/.test(text)) return "Authorization document";
  if (/declaration|form|annexure/.test(text)) return "Declaration or tender form";
  return "Bid document";
}

export function classificationRow(row) {
  return {
    logical_document_id: String(row.logical_document_id || ""),
    title: String(row.title || "Bid document"),
    document_type: String(row.document_type || "other"),
    source_document_id: String(row.source_document_id || row.document_id || ""),
    start_page: Number(row.start_page || row.page_start || 1),
    end_page: Number(row.end_page || row.page_end || row.start_page || row.page_start || 1),
    content_summary: String(row.content_summary || ""),
    key_facts: Array.isArray(row.key_facts) ? row.key_facts.map(String) : [],
    custom_document_type: String(row.custom_document_type || ""),
    broad_route: String(row.broad_route || ""),
    secondary_routes: Array.isArray(row.secondary_routes) ? row.secondary_routes.map(String) : [],
    evidence_purposes: Array.isArray(row.evidence_purposes) ? row.evidence_purposes.map(String) : [],
    route_reason: String(row.route_reason || row.reason || ""),
  };
}

export function comparableClassification(rows) {
  return (rows || []).map(classificationRow).sort(function (a, b) {
    return a.source_document_id.localeCompare(b.source_document_id) ||
      a.start_page - b.start_page || a.logical_document_id.localeCompare(b.logical_document_id);
  });
}

export function comparablePhysicalRoutes(routes) {
  return Object.keys(routes || {}).sort().map(function (documentId) {
    var route = routes[documentId] || {};
    return {
      document_id: documentId,
      broad_route: String(route.broad_route || ""),
      secondary_routes: (route.secondary_routes || []).map(String).sort(),
      reason: String(route.reason || ""),
    };
  });
}

export function classificationChanged(ctx) {
  if (!ctx.activeClassification) return false;
  return JSON.stringify(comparableClassification(ctx.activeClassification.rows)) !==
    JSON.stringify(comparableClassification(ctx.activeClassification.savedRows)) ||
    JSON.stringify(comparablePhysicalRoutes(ctx.activeClassification.physicalRoutes)) !==
    JSON.stringify(comparablePhysicalRoutes(ctx.activeClassification.savedPhysicalRoutes));
}

export function businessCategoryForType(value) {
  if (["emd_or_bid_security", "bid_security_declaration"].indexOf(value) !== -1) return "security";
  if (["past_work_evidence", "experience_po_completion", "oem_authorization"].indexOf(value) !== -1) return "pqr";
  if (["technical_proposal", "quality_certificate", "datasheet_catalogue"].indexOf(value) !== -1) return "technical";
  if (["turnover_networth_ca_certificate", "audited_financials_or_itr", "bank_solvency"].indexOf(value) !== -1) return "financial";
  if (["boq_or_price_schedule", "financial_bid", "commercial_or_price"].indexOf(value) !== -1) return "commercial";
  if (["nil_deviation_declaration", "techno_commercial_compliance"].indexOf(value) !== -1) return "techno_commercial";
  if (value === "mse_udyam_or_startup_exemption") return "security";
  if (["registration", "registration_document", "poa_or_board_authorization",
    "gst_pan_cin_statutory", "authorization_document", "consortium_jv",
    "statutory_license", "labour_compliance"].indexOf(value) !== -1) return "company";
  if (["other", "unknown"].indexOf(value) !== -1) return "";
  return "declarations";
}

export function normalizedBroadRoute(route) {
  var aliases = {
    company: "company_statutory_eligibility", company_eligibility: "company_statutory_eligibility",
    security: "emd_mse_exemptions", bid_security_exemption: "emd_mse_exemptions",
    pqr: "pqr_source_approval", technical: "technical_quality_scrutiny",
    technical_capability_past_work: "technical_quality_scrutiny", financial: "financial_qualification",
    declarations: "declarations_tender_acceptance", declarations_tender_forms: "declarations_tender_acceptance",
    techno_commercial: "techno_commercial_unpriced", commercial_document: "commercial_price_restricted",
    mixed: "mixed_submission",
  };
  return aliases[route] || route || "needs_review";
}

export function broadRouteForCategory(category) {
  var values = {
    company: "company_statutory_eligibility", security: "emd_mse_exemptions",
    pqr: "pqr_source_approval", technical: "technical_quality_scrutiny",
    financial: "financial_qualification", declarations: "declarations_tender_acceptance",
    techno_commercial: "techno_commercial_unpriced", commercial: "commercial_price_restricted",
  };
  return values[category] || category;
}

export function projectedBroadRoute(rows) {
  var routes = new Set(rows.map(function (row) {
    if (row.broad_route) return normalizedBroadRoute(row.broad_route);
    if (isReferenceDocument(row.document_type)) return "reference_document";
    var category = businessCategoryForType(row.document_type);
    return category ? broadRouteForCategory(category) : "needs_review";
  }));
  if (routes.has("needs_review") && routes.size > 1) return "mixed_submission";
  if (routes.has("commercial_price_restricted") && routes.size === 1) return "commercial_price_restricted";
  return routes.size === 1 ? Array.from(routes)[0] : "needs_review";
}

export function isReferenceDocument(value) {
  return ["tender_notice", "rfp_or_bid_document", "gem_bid", "corrigendum", "atc_stc_sla_gtc",
    "technical_specs_or_sow", "eligibility_pqc", "prebid_clarification", "contract_terms",
    "tender_reference_copy"].indexOf(value) !== -1;
}

export function containsLabel(row) {
  var labels = {
    tender_notice: "Tender notice", rfp_or_bid_document: "Tender copy", gem_bid: "GeM bid",
    corrigendum: "Corrigendum", atc_stc_sla_gtc: "Tender terms", technical_specs_or_sow: "Technical scope",
    eligibility_pqc: "Qualification criteria", boq_or_price_schedule: "Price schedule",
    forms_annexures: "Tender forms", prebid_clarification: "Pre-bid clarification", contract_terms: "Contract terms",
    tender_reference_copy: "Tender reference", bid_covering_letter: "Covering letter", technical_proposal: "Technical offer",
    financial_bid: "Price bid", emd_or_bid_security: "EMD / bid security",
    bid_security_declaration: "Bid-security declaration",
    mse_udyam_or_startup_exemption: "MSE / Udyam exemption", mii_local_content: "Local-content declaration",
    land_border_declaration: "Land-border declaration",
    oem_authorization: "OEM authorization", poa_or_board_authorization: "Authority proof",
    gst_pan_cin_statutory: "GST / PAN / company registration", turnover_networth_ca_certificate: "Turnover / net worth",
    audited_financials_or_itr: "Audited accounts / tax returns", experience_po_completion: "Work order / completion",
    past_work_evidence: "Past-work evidence", quality_certificate: "Quality certificate",
    datasheet_catalogue: "Datasheet / catalogue", integrity_blacklisting_declaration: "Integrity / non-blacklisting",
    non_blacklisting_declaration: "Non-blacklisting declaration", integrity_pact: "Integrity pact",
    nil_deviation_declaration: "Nil-deviation declaration", tender_acceptance: "Tender acceptance",
    techno_commercial_compliance: "Unpriced techno-commercial compliance",
    consortium_jv: "Consortium / joint venture", commercial_or_price: "Commercial / price",
    registration: "Company registration", registration_document: "Company registration",
    authorization_document: "Authorization", declaration: "Declaration", tender_form: "Tender form",
  };
  if (row.document_type === "other" || row.document_type === "unknown") {
    var route = normalizedBroadRoute(row.broad_route);
    var category = BUSINESS_DOCUMENT_CATEGORIES.find(function (item) {
      return broadRouteForCategory(item.value) === route;
    });
    if (route === "reference_document") return "Reference document";
    return category && route !== "needs_review" && route !== "mixed_submission"
      ? category.label
      : (row.custom_document_type || "Please check");
  }
  return labels[row.document_type] || BUSINESS_DOCUMENT_CATEGORIES.find(function (item) {
      return item.value === businessCategoryForType(row.document_type);
    }).label;
}

export function businessDocumentName(rows, document) {
  if (rows.length !== 1) return rows.length + " documents in one PDF";
  var title = String(rows[0].title || "");
  if (!title || title === document.file_name || /_[a-z]+/.test(title)) return containsLabel(rows[0]);
  return title;
}

export function businessReviewState(rows, proposalConfidence, physicalRoute) {
  var rowRoutes = rows.map(function (row) {
    return normalizedBroadRoute(row.broad_route || projectedBroadRoute([row]));
  });
  if (rowRoutes.indexOf("needs_review") !== -1) return "Please check";
  var route = (physicalRoute || {}).broad_route || projectedBroadRoute(rows);
  if (route === "reference_document") return "Reference documents";
  return "Ready";
}

export function validateClassification(ctx, byId) {
  if (!ctx.activeClassification) return "Classification is not loaded.";
  var bySource = {};
  ctx.activeClassification.rows.forEach(function (row) {
    (bySource[row.source_document_id] = bySource[row.source_document_id] || []).push(row);
  });
  for (var i = 0; i < ctx.activeClassification.documents.length; i += 1) {
    var document = ctx.activeClassification.documents[i];
    var rows = bySource[document.document_id] || [];
    var physicalRoute = ctx.activeClassification.physicalRoutes[document.document_id];
    var pages = Number(document.page_count || 0);
    if (!rows.length) return "Every file must have at least one classified section.";
    if (!physicalRoute || !physicalRoute.broad_route) return "Every file needs a classification.";
    var seen = new Array(pages + 1).fill(0);
    for (var j = 0; j < rows.length; j += 1) {
      var row = rows[j];
      var rowRoute = normalizedBroadRoute(row.broad_route || projectedBroadRoute([row]));
      if (rowRoute === "mixed_submission") return "Give each document section one classification.";
      if (rowRoute === "needs_review" && !String(row.route_reason || "").trim()) {
        return "Describe every document section that still needs classification.";
      }
      if (!row.title.trim()) return "Give every section a clear title.";
      if (!Number.isInteger(row.start_page) || !Number.isInteger(row.end_page) ||
          row.start_page < 1 || row.end_page > pages || row.start_page > row.end_page) {
        return "Page ranges must stay within the original PDF.";
      }
      for (var page = row.start_page; page <= row.end_page; page += 1) seen[page] += 1;
    }
    for (var pageNumber = 1; pageNumber <= pages; pageNumber += 1) {
      if (seen[pageNumber] !== 1) {
        return "Each page must belong to exactly one section. Fix gaps or overlapping ranges.";
      }
    }
  }
  if (classificationChanged(ctx) && !byId("classification-reason").value.trim()) {
    return "Add a short correction note before saving.";
  }
  return "";
}
