"use strict";
/* Tender compliance state and evidence helpers. */

export function complianceRunning(data, info) {
  var status = (data && data.status) || (info && info.status) || "";
  return status === "queued" || status === "running";
}

export function complianceFindings(data) {
  return data && Array.isArray(data.findings) ? data.findings : [];
}

export function findingResolutionStatus(finding) {
  return String((finding.resolution || {}).status || "open");
}

export function complianceCounts(data) {
  var findings = complianceFindings(data);
  return {
    blocking: findings.filter(function (finding) {
      return finding.status === "violation" && findingResolutionStatus(finding) === "open";
    }).length,
    review: findings.filter(function (finding) {
      return ["needs_review", "not_addressed"].indexOf(finding.status) !== -1 && findingResolutionStatus(finding) === "open";
    }).length,
    resolved: findings.filter(function (finding) {
      return ["resolved", "waived"].indexOf(findingResolutionStatus(finding)) !== -1;
    }).length,
    audited: findings.length,
  };
}

export function findingMatchesComplianceFilter(finding, filter) {
  var resolution = findingResolutionStatus(finding);
  if (filter === "blocking") return finding.status === "violation" && resolution === "open";
  if (filter === "review") return ["needs_review", "not_addressed"].indexOf(finding.status) !== -1 && resolution === "open";
  if (filter === "resolved") return resolution === "resolved" || resolution === "waived";
  return true;
}

export function coverageList(data, summary, coverage, name) {
  var value = coverage[name] || summary[name] || data[name] || [];
  return Array.isArray(value) ? value : [];
}

export function isInternalEvidenceId(value) {
  var text = String(value || "").trim();
  if (!text) return true;
  return /^(anchor-|defect-|planted-)/i.test(text)
    || /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(text)
    || text === "planted-defect";
}

export function isNoisyTenderQuote(quote) {
  var text = String(quote || "").trim();
  if (!text) return true;
  if (/^\d{5,}[\/-]/.test(text)) return true;
  if (/^hw\d+/i.test(text)) return true;
  if (text.length < 8) return true;
  var latin = (text.match(/[A-Za-z0-9]/g) || []).length;
  if (latin < text.length * 0.4) return true;
  return false;
}

export function cleanTenderQuote(raw) {
  var text = String(raw || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  var source = text.match(/source quote:\s*(.+)$/i);
  if (source) text = source[1].trim();
  else {
    var fact = text.match(/tender fact:\s*([^|]+)/i);
    if (fact) text = fact[1].trim();
  }
  text = text.replace(/^relevant fields:\s*/i, "");
  text = text.replace(/\s*\|\s*/g, " · ");
  text = text.replace(/^(tender fact|condition or exception|evaluation rule|proof requirement)\s*[:·]\s*/i, "");
  text = text.replace(/EMD Detail\s*[·:\/]\s*Required\s*[·:\/]\s*No/ig, "EMD is not required");
  text = text.replace(/ePBG Detail\s*[·:\/]\s*Required\s*[·:\/]\s*No/ig, "ePBG is not required");
  text = text.replace(/ePBG Percentage\([^)]*\)\s*[·:]\s*NA/ig, "ePBG is not applicable");
  text = text.replace(/EMD is not required\s*ePBG is not required/i, "EMD is not required. ePBG is not required.");
  text = text.replace(/\s*·\s*$/, "");
  return text.trim();
}

export function evidenceSourceLabel(item) {
  var named = String(item.document_name || item.source_name || "").trim();
  if (named && !isInternalEvidenceId(named)) return named;
  var unitDocument = String(item.unit_id || "").match(/^(.*)-p\d+(?:-|$)/);
  var documentId = String(item.document_id || (unitDocument ? unitDocument[1] : "")).trim();
  if (documentId && !isInternalEvidenceId(documentId) && !/^anchor-/i.test(documentId)) {
    return documentId;
  }
  return "Tender";
}

export function cleanedEvidence(finding) {
  var seen = Object.create(null);
  var items = [];
  (Array.isArray(finding.tender_evidence) ? finding.tender_evidence : []).forEach(function (item) {
    if (!item || typeof item !== "object") return;
    var quote = cleanTenderQuote(item.quote);
    if (!quote || isNoisyTenderQuote(quote)) return;
    var key = quote.toLowerCase();
    if (seen[key]) return;
    seen[key] = true;
    items.push({
      quote: quote,
      page: item.page,
      document_id: item.document_id,
      unit_id: item.unit_id,
      document_name: item.document_name,
      source_name: item.source_name,
      label: evidenceSourceLabel(item),
    });
  });
  return items.slice(0, 2);
}

export function problemBullets(finding) {
  if (Array.isArray(finding.officer_problem) && finding.officer_problem.length) {
    return finding.officer_problem.map(String).filter(Boolean);
  }
  return [];
}

export function actionBullets(finding) {
  if (Array.isArray(finding.officer_actions) && finding.officer_actions.length) {
    return finding.officer_actions.map(String).filter(Boolean);
  }
  return [];
}

export function isGenericLockText(text) {
  return /evaluation checklist stays locked/i.test(String(text || ""));
}

export function resultAlreadyOnFile(check) {
  var card = check && check.next_ask && typeof check.next_ask === "object" ? check.next_ask : {};
  if (Array.isArray(card.already_have) && card.already_have.length) {
    return card.already_have.map(String).filter(function (item) { return item && !/^Bid quote:/i.test(item); });
  }
  var have = [];
  (check && check.claims || []).forEach(function (claim) {
    var name = String(claim.fact_name || "").trim();
    var value = String(claim.value || "").replace(/\s+/g, " ").trim();
    if (name && value && value.length <= 140) have.push(name + ": " + value);
    else if (name) have.push(name);
  });
  return have;
}

export function resultPolicyLine(check, criterionKind) {
  var card = check && check.next_ask && typeof check.next_ask === "object" ? check.next_ask : {};
  if (card.next_action && card.next_action.label) return String(card.next_action.label);
  if (card.missing_paper) return String(card.missing_paper);
  if (Array.isArray(card.officer_actions) && card.officer_actions[0]) return String(card.officer_actions[0]);
  if (Array.isArray(card.already_have) && card.already_have[0]) return String(card.already_have[0]);
  var have = resultAlreadyOnFile(check);
  if (have[0]) return have[0];
  if (criterionKind && criterionKind(check) !== "pass" && check.required_proof) return String(check.required_proof);
  return "";
}
