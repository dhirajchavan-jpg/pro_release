"use strict";
/* Checklist data normalization and display helpers. */

export var CHECKLIST_GROUPS = {
  eligibility: {
    label: "Eligibility and legal status",
    description: "Who may bid, required registrations, bidder structure, experience, and similar-work qualification.",
  },
  financial: {
    label: "Financial qualification",
    description: "Turnover, net worth, audited statements, and other financial capacity requirements.",
  },
  compliance: {
    label: "Statutory and policy compliance",
    description: "Declarations, Make in India and MSE rules, statutory obligations, signing, and debarment checks.",
  },
  technical: {
    label: "Technical capability and quality",
    description: "Technical specifications, manufacturing capacity, quality controls, inspections, and scored checks.",
  },
  commercial: {
    label: "Commercial and bid terms",
    description: "Bid security, fees, exemptions, and purchase-preference controls. Price evaluation remains outside this technical review.",
  },
  other: {
    label: "Other evaluation checks",
    description: "Additional tender-specific checks that do not fit the standard procurement categories.",
  },
};

export var CHECKLIST_GROUP_ORDER = ["eligibility", "financial", "compliance", "technical", "commercial", "other"];

export var MATERIALITY_LABELS = {
  mandatory_blocking: "Must pass",
  conditional_blocking: "Conditional",
  scored: "Scored",
};

export var FAILURE_EFFECT_LABELS = {
  reject_unresponsive: "Failure may disqualify",
  manual_review: "Failure requires review",
  score_only: "Affects score",
};

export var ORIGIN_LABELS = {
  tender_derived: "Derived from tender",
  human_added: "Added by reviewer",
};

export var RULE_LABELS = {
  accepted_scope_keywords: "Accepted similar-work descriptions",
  accepted_statutory_identifier_types: "Accepted registrations",
  allowed_participation_modes: "Allowed bidder participation",
  beneficiary_name: "EMD beneficiary name",
  declaration_type: "Declaration to check",
  domestic_country_or_jurisdiction: "Required country or jurisdiction",
  eligibility_cutoff_date: "Eligibility cut-off date",
  emd_amount_inr: "EMD amount (INR)",
  evaluation_cutoff_date: "Financial evaluation cut-off date",
  exemption_requires: "Documents required for EMD exemption",
  financial_year_start_day: "Financial year start day",
  financial_year_start_month: "Financial year start month",
  lookback_end_date: "Past-work lookback end date",
  lookback_years: "Past-work lookback period (years)",
  minimum_average_turnover_minor: "Minimum average turnover",
  minimum_completed_full_financial_years: "Minimum completed financial years",
  minimum_electrical_component_inr_including_gst: "Minimum electrical component value including GST",
  required_audited_years: "Number of audited years required",
  tender_form_reference: "Required tender form / annexure",
  tender_issue_date: "Tender issue date",
  tender_reference: "Tender reference",
  allowed_legal_entities: "Allowed bidder legal forms",
  joint_venture_allowed: "Joint ventures allowed",
  local_content_declaration: "Local-content declaration",
  min_turnover_req_inr: "Minimum turnover",
  mse_startup_relaxation: "MSE/startup EMD exemption",
  net_worth_requirement: "Net worth requirement",
  publish_date: "Tender publication date",
  similar_works_milestones: "Similar-work experience thresholds",
  statutory_registrations_required: "Required registrations",
  tender_id: "Tender number",
};

export function normalizeDraft(ctx) {
  ctx.draft = ctx.draft || {};
  ctx.draft.revision = Number(ctx.draft.revision || 0);
  ctx.draft.criteria = Array.isArray(ctx.draft.criteria) ? ctx.draft.criteria : [];
  ctx.draft.criteria.forEach(function (criterion) {
    criterion.rule = criterion.rule && typeof criterion.rule === "object" ? criterion.rule : {};
    criterion.missing_fields = Array.isArray(criterion.missing_fields) ? criterion.missing_fields : [];
    criterion.depends_on = Array.isArray(criterion.depends_on) ? criterion.depends_on : [];
    criterion.rfp_source_refs = Array.isArray(criterion.rfp_source_refs) ? criterion.rfp_source_refs : [];
    criterion.review = criterion.review && typeof criterion.review === "object" ? criterion.review : {};
    if (typeof criterion.review.included !== "boolean") criterion.review.included = true;
    criterion.review.origin = criterion.review.origin || "tender_derived";
    criterion.review.exclusion_reason = criterion.review.exclusion_reason || "";
    criterion.review.change_reason = criterion.review.change_reason || "";
    criterion.review.confirmed = criterion.review.included !== false && criterion.review.confirmed === true;
    criterion.review.confirmed_by = criterion.review.confirmed ? (criterion.review.confirmed_by || "") : "";
    criterion.review.confirmed_at = criterion.review.confirmed ? (criterion.review.confirmed_at || "") : "";
  });
  var criterionIds = Object.create(null);
  ctx.draft.criteria.forEach(function (criterion) { criterionIds[criterion.criterion_id] = true; });
  ctx.draft.evaluation_groups = Array.isArray(ctx.draft.evaluation_groups)
    ? ctx.draft.evaluation_groups.map(function (group) {
        return Object.assign({}, group, {
          criterion_ids: (group.criterion_ids || []).filter(function (criterionId) { return criterionIds[criterionId]; }),
        });
      }).filter(function (group) { return group.criterion_ids.length; })
    : [];
  var groupedIds = Object.create(null);
  ctx.draft.evaluation_groups.forEach(function (group) {
    group.criterion_ids.forEach(function (criterionId) { groupedIds[criterionId] = true; });
  });
  ctx.draft.criteria.forEach(function (criterion) {
    if (groupedIds[criterion.criterion_id]) return;
    ctx.draft.evaluation_groups.push({
      group_id: "GROUP-" + criterion.criterion_id,
      title: criterion.title || "Evaluation requirement",
      criterion_ids: [criterion.criterion_id],
      source_document_id: "",
      source_page: null,
      evidence_set: [],
    });
  });
}

export function evaluationCheckGroups(draft) {
  return draft && Array.isArray(draft.evaluation_groups) ? draft.evaluation_groups : [];
}

export function criterionIncluded(criterion) {
  return !criterion.review || criterion.review.included !== false;
}

export function criterionConfirmed(criterion) {
  return criterionIncluded(criterion) && Boolean(criterion.review && criterion.review.confirmed);
}

export function criterionNeedsAttention(criterion) {
  if (criterionConfirmed(criterion)) return false;
  var humanRequirementMissing =
    criterion.review &&
    criterion.review.origin === "human_added" &&
    !String((criterion.rule || {}).requirement || "").trim();
  return criterionIncluded(criterion) && Boolean(
    humanRequirementMissing ||
    criterion.needs_human_input ||
    (criterion.missing_fields && criterion.missing_fields.length) ||
    String(criterion.applicability || "").toLowerCase() === "unresolved"
  );
}

export function coerceRuleValue(raw, original) {
  if (typeof original === "boolean") return raw === "true";
  if (typeof original === "number") {
    var num = Number(raw);
    if (raw === "" || !isFinite(num)) throw new Error("not a number");
    return num;
  }
  if (Array.isArray(original)) {
    if (original.every(function (item) { return typeof item !== "object"; })) {
      return raw
        .split(",")
        .map(function (part) { return part.trim(); })
        .filter(function (part) { return part !== ""; })
        .map(function (part) {
          if (original.length && original.every(function (item) { return typeof item === "number"; })) {
            var n = Number(part);
            if (!isFinite(n)) throw new Error("not a number");
            return n;
          }
          return part;
        });
    }
    return JSON.parse(raw);
  }
  if (original !== null && typeof original === "object") return JSON.parse(raw);
  return raw;
}

export function ruleValueToText(value) {
  if (Array.isArray(value)) {
    if (value.every(function (item) { return typeof item !== "object"; })) {
      return value.join(", ");
    }
    return JSON.stringify(value, null, 2);
  }
  if (value !== null && typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export function ledgerValueToText(value, indent) {
  indent = indent || "";
  if (Array.isArray(value)) {
    return value.map(function (item) {
      return item !== null && typeof item === "object"
        ? "\n" + ledgerValueToText(item, indent + "  ")
        : String(item);
    }).join(", ").trim();
  }
  if (value !== null && typeof value === "object") {
    return Object.keys(value).map(function (key) {
      var item = value[key];
      var nested = item !== null && typeof item === "object";
      return indent + friendlyKey(key) + (nested ? ":\n" : ": ") +
        ledgerValueToText(item, nested ? indent + "  " : "");
    }).join("\n");
  }
  return String(value);
}

export function friendlyKey(key) {
  if (RULE_LABELS[key]) return RULE_LABELS[key];
  return key.replace(/_/g, " ").replace(/^./, function (letter) { return letter.toUpperCase(); });
}

export function friendlyStatus(status) {
  if (!status) return "not recorded";
  var labels = {
    complete: "Complete",
    conflict: "Conflicting evidence",
    failed: "Failed",
    found: "Found",
    need_more_evidence: "More evidence needed",
    not_applicable: "Not applicable",
    not_found: "Not found",
    partial: "Partially complete",
    unreadable_or_not_checked: "Unreadable or not checked",
    unresolved: "Unresolved",
  };
  return labels[status] || status.replace(/_/g, " ");
}

export function criterionGroup(criterion) {
  var family = String(criterion.family || "other");
  return CHECKLIST_GROUPS[family] ? family : "other";
}

export function criterionDisplayParts(criterion) {
  var text = String(criterion.title || "Untitled evaluation check").trim();
  var divider = text.indexOf(" — ");
  var topic = divider === -1 ? (CHECKLIST_GROUPS[criterionGroup(criterion)] || CHECKLIST_GROUPS.other).label : text.slice(0, divider);
  var requirement = divider === -1 ? text : text.slice(divider + 3);
  [". Conditions:", ". Exceptions:"].forEach(function (marker) {
    var markerIndex = requirement.indexOf(marker);
    if (markerIndex !== -1) requirement = requirement.slice(0, markerIndex);
  });
  requirement = requirement.replace(/\.\.$/, ".").trim();
  return { topic: topic, requirement: requirement || text };
}

export function criterionTerms(criterion) {
  var text = String((criterion.rule || {}).requirement || criterion.title || "");
  var conditions = [];
  var exceptions = [];
  var conditionStart = text.indexOf("Conditions:");
  var exceptionStart = text.indexOf("Exceptions:");
  if (conditionStart !== -1) {
    var conditionText = text.slice(conditionStart + 11, exceptionStart === -1 ? text.length : exceptionStart);
    conditions = conditionText.split(";").map(function (item) { return item.replace(/^\s*[.]+|[.]+\s*$/g, "").trim(); }).filter(Boolean);
  }
  if (exceptionStart !== -1) {
    exceptions = text.slice(exceptionStart + 11).split(";").map(function (item) { return item.replace(/^\s*[.]+|[.]+\s*$/g, "").trim(); }).filter(Boolean);
  }
  return { conditions: conditions, exceptions: exceptions };
}

export function criterionImpact(criterion) {
  if (!criterionIncluded(criterion)) return "Excluded from evaluation";
  if (criterion.materiality === "scored") return "Affects score";
  return FAILURE_EFFECT_LABELS[String((criterion.rule || {}).failure_effect || "")] ||
    MATERIALITY_LABELS[String(criterion.materiality || "")] ||
    "Requires evaluator review";
}

export function missingParts(item) {
  var match = /^(field|rule):([^ ]+) \((.*)\)$/.exec(item);
  if (!match) return null;
  var kind = match[1];
  var key = match[2];
  if (kind === "rule" && key.indexOf("FIELD-") === 0) {
    kind = "field";
    key = key.slice(6);
  }
  return { kind: kind, key: key, reason: match[3] };
}

export function friendlyMissing(item) {
  var parts = missingParts(item);
  if (!parts) return item.replace(/_/g, " ");
  var reasons = {
    "no source field": "not stated in the tender findings",
    "ledger status not_found": "Not found in the tender",
    "ledger status unresolved": "Could not be determined from the tender",
    "field value is not verified": "the extracted value is not verified",
    "quote cannot be grounded": "the value is not tied to an exact tender quote",
    "grounding review required": "the cited evidence conflicts with or does not support the value",
  };
  return friendlyKey(parts.key) + " — " + (reasons[parts.reason] || parts.reason.replace(/_/g, " "));
}

export function criterionTitle(ctx, criterionId) {
  var match = ctx.draft.criteria.find(function (item) { return item.criterion_id === criterionId; });
  return match ? match.title : criterionId;
}
