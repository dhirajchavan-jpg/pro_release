"use strict";
/* Evaluation result state helpers. */

export function resultKind(data) {
  if (data.criteria && data.criteria.length) {
    var checks = [];
    data.criteria.forEach(function (criterion) { checks = checks.concat(criterion.checks || []); });
    var kinds = checks.length ? checks.map(criterionKind) : data.criteria.map(criterionKind);
    if (kinds.indexOf("fail") !== -1) return "fail";
    if (data.criteria.some(function (criterion) { return criterion.evaluation_state === "not_evaluated_after_blocker"; })) return "review";
    if (kinds.every(function (kind) { return kind === "pass"; })) return "pass";
  }
  if (data.recommendation === "regression_gates_clear") return "pass";
  if (data.recommendation === "regression_gate_failed") return "fail";
  return "review";
}

export function resultLabel(kind) {
  return kind === "pass" ? "Meets evaluated requirements" :
    kind === "fail" ? "Does not qualify" : "Human review required";
}

export function displayReason(value) {
  return String(value || "")
    .replace(/\b[A-Za-z0-9._/-]+ criterion judge:\s*/gi, "")
    .replace(/\bDeterministic (?:validator|corroboration):\s*/gi, "");
}

export function resultExplanation(kind) {
  return kind === "pass"
    ? "All evaluated mandatory requirements passed. Named committee approval is still required before any commercial action."
    : kind === "fail"
      ? "One or more mandatory requirements were not demonstrated by the submitted bid."
      : "The submitted evidence does not support a defensible pass or fail decision yet.";
}

export function criterionKind(criterion) {
  if (criterion.human_review) {
    if (criterion.human_review.decision === "meets_requirement") return "pass";
    if (criterion.human_review.decision === "does_not_meet") return "fail";
    return "review";
  }
  if (criterion.status === "not_evaluated" || String(criterion.evaluation_state || "").indexOf("not_evaluated") === 0) return "not_evaluated";
  if (["pass", "passed", "qualified", "compliant", "not_applicable"].indexOf(criterion.status) !== -1) return "pass";
  if (["fail", "failed", "fail_fact", "present_but_invalid", "regression_gate_failed"].indexOf(criterion.status) !== -1) return "fail";
  return "review";
}

export function criterionLabel(kind, criterion) {
  if (criterion.human_review) {
    return criterion.human_review.decision === "meets_requirement" ? "Human confirmed: meets" :
      criterion.human_review.decision === "does_not_meet" ? "Human confirmed: fails" : "Clarification required";
  }
  if (criterion.evaluation_state === "not_evaluated_system") return "Processing incomplete";
  if (String(criterion.evaluation_state || "").indexOf("not_evaluated") === 0) return "Not evaluated";
  if (criterion.status === "not_proven") return "Not proven";
  if (criterion.status === "conflict") return "Conflicting";
  if (criterion.status === "missing_after_complete_search" || criterion.status === "missing") return "Missing";
  return kind === "pass" ? "Meets requirement" : kind === "fail" ? "Does not meet requirement" : "Needs review";
}

export function criterionTitle(criterion, friendlyKey) {
  var titles = {
    entity: "Bidder identity and eligibility",
    emd: "Bid security / EMD",
    similar_work: "Past work experience",
    financial: "Financial qualification",
    declaration: "Mandatory declaration",
  };
  return titles[criterion.family] || friendlyKey(criterion.criterion_id);
}

export function reviewPanelId(vendor, criterionId) {
  var checkpointId = arguments.length > 2 ? arguments[2] : "";
  return "human-review-" + (vendor + "-" + criterionId + "-" + checkpointId).replace(/[^A-Za-z0-9_-]+/g, "-");
}

export function resultChecks(data) {
  var checks = [];
  (data.criteria || []).forEach(function (criterion) {
    (criterion.checks || [criterion]).forEach(function (check) {
      if (!check.family) check.family = criterion.family || "other";
      checks.push(check);
    });
  });
  return checks;
}

export function resultCheckKey(check, index) {
  return String(check.criterion_id || "criterion-" + index) + "::" + String(check.checkpoint_id || "");
}

export function resultCounts(checks) {
  var counts = { total: checks.length, pass: 0, fail: 0, review: 0, not_evaluated: 0 };
  checks.forEach(function (check) { counts[criterionKind(check)] += 1; });
  return counts;
}

export function shortId(value) {
  return value.length > 24 ? value.slice(0, 24) + "…" : value;
}
