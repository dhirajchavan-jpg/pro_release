"use strict";
/* Tender compliance loading, resolution, and re-upload actions. */

import { complianceRunning } from "./compliance-model.js";

export function installComplianceActions(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var complianceInfo = function () { return ctx.complianceInfo.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var refreshState = function () { return ctx.refreshState.apply(ctx, arguments); };
  var renderCompliance = function () { return ctx.renderCompliance.apply(ctx, arguments); };
  var renderMessage = function () { return ctx.renderMessage.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };
  var waitForWorkbenchJob = function () { return ctx.waitForWorkbenchJob.apply(ctx, arguments); };

  function loadCompliance() {
    if (!ctx.projectId) return Promise.resolve();
    if (ctx.complianceReloadTimer) {
      window.clearTimeout(ctx.complianceReloadTimer);
      ctx.complianceReloadTimer = null;
    }
    if (!complianceInfo() || !complianceInfo().enabled) {
      ctx.complianceData = null;
      renderCompliance(null);
      return Promise.resolve();
    }
    setStatusText("compliance-status", "Loading compliance audit…");
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/compliance").then(function (res) {
      if (!res.ok) {
        ctx.complianceData = null;
        renderCompliance(null);
        renderMessage(byId("compliance-errors"), apiError(res.data, "The compliance audit could not be loaded."), "error");
        return;
      }
      ctx.complianceData = res.data;
      renderCompliance(res.data);
      if (complianceRunning(res.data, complianceInfo())) {
        ctx.complianceReloadTimer = window.setTimeout(loadCompliance, 3000);
      }
    });
  }

  function runCompliance() {
    if (!ctx.projectId) return;
    renderMessage(byId("compliance-errors"), "");
    byId("run-compliance-btn").disabled = true;
    showOperationActivity("compliance-status", "compliance", {
      title: "Reviewing tender compliance",
      status: "queued",
      detail: "Starting the policy audit…",
      next: "You can leave this page. Findings will appear here automatically.",
    });
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/compliance/run", {
      method: "POST",
    }).then(function (res) {
      if (!res.ok) {
        byId("run-compliance-btn").disabled = false;
        setStatusText("compliance-status", "");
        renderMessage(byId("compliance-errors"), apiError(res.data, "Could not start the compliance audit."), "error");
        return;
      }
      renderMessage(byId("compliance-errors"), "");
      waitForWorkbenchJob(res.data.job_id, {
        kind: "compliance",
        failureMessage: "Tender audit failed.",
        statusElement: byId("compliance-status"),
        onUpdate: function (job, status) {
          showOperationActivity("compliance-status", "compliance", {
            title: "Reviewing tender compliance",
            payload: job,
            status: status === "status_unavailable" || status === "stalled" ? "running" : "",
            detail: status === "status_unavailable"
              ? "Status is temporarily unavailable; the audit may still be running."
              : status === "stalled"
              ? "This is taking longer than usual, but the audit is still running."
              : "",
            next: "You can leave this page. Findings will appear here automatically.",
          });
        },
      }).then(function () {
        return refreshState();
      }).then(function () {
        loadCompliance();
        showMessage("Tender audit finished. Review its coverage and findings before continuing.", "ok");
      }).catch(function (errorValue) {
        showOperationActivity("compliance-status", "compliance", {
          title: "Tender compliance audit failed",
          status: "failed",
          detail: errorValue.message || "Tender audit failed.",
          next: "Review the error, then run the audit again.",
        });
        showMessage(errorValue.message || "Tender audit failed.", "error");
        loadCompliance();
      });
    });
  }

  function selectCompliancePolicy(policyId) {
    if (!ctx.projectId || !policyId) return Promise.resolve();
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/compliance/policy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy_id: policyId }),
    }).then(function (res) {
      if (!res.ok) {
        renderMessage(byId("compliance-errors"), apiError(res.data, "The governing policy could not be selected."), "error");
        return;
      }
      showMessage("Governing policy selected. Run the tender audit again.", "ok");
      return refreshState().then(loadCompliance);
    });
  }

  function resolveComplianceFinding(findingId, action, note, approvalAuthority, approvalReference) {
    if (!ctx.projectId || !findingId) return;
    ctx.complianceResolutionInFlight = ctx.complianceResolutionInFlight || Object.create(null);
    var requestKey = findingId + ":" + action;
    if (ctx.complianceResolutionInFlight[requestKey]) return;
    if (action === "waive" && !note) {
      renderMessage(byId("compliance-errors"), "A note is required to waive a finding.", "error");
      return;
    }
    if (action === "waive" && (!approvalAuthority || !approvalReference)) {
      renderMessage(byId("compliance-errors"), "Select the approving authority and enter the approval reference.", "error");
      return;
    }
    ctx.complianceResolutionInFlight[requestKey] = true;
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) +
      "/compliance/findings/" + encodeURIComponent(findingId) + "/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: action,
        note: note || "",
        approval_authority: approvalAuthority || "",
        approval_reference: approvalReference || "",
        resolved_by: reviewer(),
        resolved_at: nowIso(),
      }),
    }).then(function (res) {
      if (!res.ok) {
        renderMessage(byId("compliance-errors"), apiError(res.data, "The finding could not be updated."), "error");
        return;
      }
      showMessage("Finding updated.", "ok");
      return refreshState().then(loadCompliance);
    }).finally(function () {
      delete ctx.complianceResolutionInFlight[requestKey];
    });
  }

  function reuploadTender() {
    if (!ctx.projectId) return;
    var input = byId("compliance-reupload-file");
    var file = input.files[0];
    if (!file) {
      renderMessage(byId("compliance-errors"), "Choose the corrected tender file first.", "error");
      return;
    }
    if (!window.confirm("This replaces the tender document and resets understanding/contract work. Continue?")) return;
    var reason = window.prompt("Reason for the re-upload (optional):") || "";
    var form = new FormData();
    form.append("file", file);
    if (reason.trim()) form.append("reason", reason.trim());
    byId("compliance-reupload-btn").disabled = true;
    showMessage("Uploading the corrected tender…");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/tender/reupload", {
      method: "POST",
      body: form,
    }).then(function (res) {
      byId("compliance-reupload-btn").disabled = false;
      if (!res.ok) {
        renderMessage(byId("compliance-errors"), apiError(res.data, "The tender could not be replaced."), "error");
        return;
      }
      input.value = "";
      refreshState().then(function () {
        showMessage("Tender replaced. Run the tender analysis again.", "ok");
        window.location.href = pageUrl("tender");
      });
    });
  }

  return {
    loadCompliance: loadCompliance,
    runCompliance: runCompliance,
    selectCompliancePolicy: selectCompliancePolicy,
    resolveComplianceFinding: resolveComplianceFinding,
    reuploadTender: reuploadTender,
  };
}
