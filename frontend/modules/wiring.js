"use strict";
/* DOM event wiring and bootstrap. */

export function installWiring(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var accountMessage = function () { return ctx.accountMessage.apply(ctx, arguments); };
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var applyPageMode = function () { return ctx.applyPageMode.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var navigateWorkflow = function () { return ctx.navigateWorkflow.apply(ctx, arguments); };
  var projectStageLabel = function () { return ctx.projectStageLabel.apply(ctx, arguments); };
  var renderAccount = function () { return ctx.renderAccount.apply(ctx, arguments); };
  var renderPortfolio = function () { return ctx.renderPortfolio.apply(ctx, arguments); };
  var routeProject = function () { return ctx.routeProject.apply(ctx, arguments); };
  var showAuth = function () { return ctx.showAuth.apply(ctx, arguments); };
  var showProjectContext = function () { return ctx.showProjectContext.apply(ctx, arguments); };
  var showRouteLoading = function () { return ctx.showRouteLoading.apply(ctx, arguments); };
  var showSetupTab = function () { return ctx.showSetupTab.apply(ctx, arguments); };
  var showWorkflow = function () { return ctx.showWorkflow.apply(ctx, arguments); };
  var submitAccount = function () { return ctx.submitAccount.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  var useDefaultSchema = function () { return ctx.useDefaultSchema.apply(ctx, arguments); };
  var addBid = function () { return ctx.addBid.apply(ctx, arguments); };
  var addChecklistCriterion = function () { return ctx.addChecklistCriterion.apply(ctx, arguments); };
  var approveClassification = function () { return ctx.approveClassification.apply(ctx, arguments); };
  var attemptContinue = function () { return ctx.attemptContinue.apply(ctx, arguments); };
  var complianceInfo = function () { return ctx.complianceInfo.apply(ctx, arguments); };
  var continueToEvaluation = function () { return ctx.continueToEvaluation.apply(ctx, arguments); };
  var createProject = function () { return ctx.createProject.apply(ctx, arguments); };
  var download = function () { return ctx.download.apply(ctx, arguments); };
  var downloadExcel = function () { return ctx.downloadExcel.apply(ctx, arguments); };
  var downloadJson = function () { return ctx.downloadJson.apply(ctx, arguments); };
  var downloadMarkdown = function () { return ctx.downloadMarkdown.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var handleEvaluationAction = function () { return ctx.handleEvaluationAction.apply(ctx, arguments); };
  var improveClassification = function () { return ctx.improveClassification.apply(ctx, arguments); };
  var loadAccount = function () { return ctx.loadAccount.apply(ctx, arguments); };
  var loadCompliance = function () { return ctx.loadCompliance.apply(ctx, arguments); };
  var loadDefaultSchemaInfo = function () { return ctx.loadDefaultSchemaInfo.apply(ctx, arguments); };
  var loadDraft = function () { return ctx.loadDraft.apply(ctx, arguments); };
  var loadLedger = function () { return ctx.loadLedger.apply(ctx, arguments); };
  var loadManualBidReview = function () { return ctx.loadManualBidReview.apply(ctx, arguments); };
  var loadResults = function () { return ctx.loadResults.apply(ctx, arguments); };
  var loadReviewLog = function () { return ctx.loadReviewLog.apply(ctx, arguments); };
  var openChecklist = function () { return ctx.openChecklist.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var reuploadTender = function () { return ctx.reuploadTender.apply(ctx, arguments); };
  var renderBidderComparison = function () { return ctx.renderBidderComparison.apply(ctx, arguments); };
  var renderDraft = function () { return ctx.renderDraft.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var runCompliance = function () { return ctx.runCompliance.apply(ctx, arguments); };
  var runEvaluate = function () { return ctx.runEvaluate.apply(ctx, arguments); };
  var runIntake = function () { return ctx.runIntake.apply(ctx, arguments); };
  var runUnderstanding = function () { return ctx.runUnderstanding.apply(ctx, arguments); };
  var saveClassification = function () { return ctx.saveClassification.apply(ctx, arguments); };
  var saveContractDraft = function () { return ctx.saveContractDraft.apply(ctx, arguments); };
  var selectReportVendors = function () { return ctx.selectReportVendors.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var syncReviewerIdentity = function () { return ctx.syncReviewerIdentity.apply(ctx, arguments); };
  var updateClassificationState = function () { return ctx.updateClassificationState.apply(ctx, arguments); };
  var useSchemaText = function () { return ctx.useSchemaText.apply(ctx, arguments); };
  // ---------------------------------------------------------------- wiring

  function hasUnsavedWork() {
    return Boolean(ctx.draftDirty || (ctx.classificationChanged && ctx.classificationChanged()));
  }

  function confirmDiscardUnsaved() {
    return !hasUnsavedWork() || window.confirm("Leave without saving your changes?");
  }

  function init() {
    var route = routeProject();
    showRouteLoading(route
      ? "Loading " + (ctx.PAGE_NAMES.indexOf(route.page) >= 0 ? byId("pill-" + (ctx.PAGE_NAMES.indexOf(route.page) + 1)).lastElementChild.textContent.toLowerCase() : "workspace")
      : "Loading workspace");
    applyPageMode(route ? route.page : "tender");
    window.addEventListener("beforeunload", function (event) {
      if (!hasUnsavedWork()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    window.addEventListener("popstate", function () {
      var nextRoute = routeProject();
      if (!nextRoute || nextRoute.projectId !== ctx.projectId || ctx.draftDirty) {
        window.location.reload();
        return;
      }
      navigateWorkflow(nextRoute.page, window.location.href, false);
    });
    byId("register-btn").addEventListener("click", function () {
      var button = byId("register-btn");
      if (button.disabled) return;
      button.disabled = true;
      submitAccount("/workbench/account/register", {
        name: byId("register-name").value.trim(),
        email: byId("register-email").value.trim(),
        phone: byId("register-phone").value.trim(),
        password: byId("register-password").value,
      }).finally(function () { button.disabled = false; });
    });
    byId("login-btn").addEventListener("click", function () {
      var button = byId("login-btn");
      if (button.disabled) return;
      button.disabled = true;
      submitAccount("/workbench/account/login", {
        username: byId("login-username").value.trim(),
        password: byId("login-password").value,
      }).finally(function () { button.disabled = false; });
    });
    byId("save-profile-btn").addEventListener("click", function () {
      api("/workbench/account", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: byId("profile-name").value.trim(), phone: byId("profile-phone").value.trim() }),
      }).then(function (res) {
        if (res.ok) renderAccount(res.data);
        accountMessage(res.ok ? "Profile saved." : apiError(res.data, "Could not save profile."), res.ok ? "ok" : "error");
      });
    });
    byId("logout-btn").addEventListener("click", function () {
      api("/workbench/account/logout", { method: "POST" }).then(function () {
        window.history.replaceState({}, "", "/workbench");
        window.history.replaceState({}, "", "/workbench/login");
        showAuth("login");
      });
    });
    byId("new-tender-btn").addEventListener("click", function () {
      if (!confirmDiscardUnsaved()) return;
      ctx.projectId = null;
      ctx.state = null;
      ctx.complianceData = null;
      ctx.tenderDocuments = null;
      ctx.resultsLoadedProject = null;
      ctx.reportLoadedProject = null;
      window.history.replaceState({}, "", "/workbench");
      byId("project-id").value = "";
      byId("tender-file").value = "";
      byId("policy-audit-toggle").checked = false;
      byId("selected-tender-name").textContent = "Not selected";
      byId("create-project-btn").hidden = false;
      byId("create-project-btn").disabled = true;
      byId("run-understanding-btn").hidden = true;
      byId("run-understanding-btn").disabled = true;
      setStatusText("understand-status", "");
      showMessage("");
      useDefaultSchema();
      showSetupTab("package");
      showProjectContext(null);
      showWorkflow("tender");
      updateStepper();
    });
    document.querySelectorAll(".step-pill").forEach(function (link) {
      link.addEventListener("click", function (event) {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (!confirmDiscardUnsaved()) return;
        var target = new URL(link.href, window.location.origin);
        navigateWorkflow(target.pathname.split("/").pop(), target.pathname + target.search, true);
      });
    });
    byId("portfolio-toggle-btn").addEventListener("click", function () {
      var params = new URLSearchParams(window.location.search);
      window.location.href = params.get("tender") ? "/workbench?view=portfolio" :
        params.get("view") === "portfolio" || params.get("portfolio") === "demo"
          ? "/workbench" : "/workbench?view=portfolio";
    });
    byId("portfolio-search").addEventListener("input", renderPortfolio);
    byId("portfolio-status").addEventListener("change", renderPortfolio);
    byId("portfolio-export-btn").addEventListener("click", function () {
      var rows = [["Tender number", "Tender title", "Stage", "Bidders", "Qualified", "Failed", "Needs review", "Status"]];
      ctx.portfolioProjects.forEach(function (project) {
        var summary = project.portfolio_summary || {};
        rows.push([
          project.tender_number || project.project_id,
          project.title || "Untitled tender",
          projectStageLabel(project.stage),
          summary.bidder_count || 0,
          summary.qualified_count || 0,
          summary.failed_count || 0,
          summary.review_count || 0,
          summary.status || "In progress",
        ]);
      });
      var csv = rows.map(function (row) {
        return row.map(function (value) {
          var safe = String(value);
          if (/^[=+\-@]/.test(safe.trimStart())) safe = "'" + safe;
          return '"' + safe.replace(/"/g, '""') + '"';
        }).join(",");
      }).join("\r\n");
      var url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      var link = el("a", { href: url, download: "procurement-portfolio.csv" });
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    });
    byId("bidder-back-btn").addEventListener("click", function () {
      window.location.href = "/workbench?view=portfolio";
    });
    byId("bidder-search").addEventListener("input", renderBidderComparison);
    byId("bidder-status").addEventListener("change", renderBidderComparison);
    byId("bidder-report-btn").addEventListener("click", function () {
      if (!ctx.portfolioProject) return;
      download(
        "/workbench/projects/" + encodeURIComponent(ctx.portfolioProject.project_id) + "/report/excel",
        "bid-evaluation-" + ctx.portfolioProject.project_id + ".xlsx"
      );
    });
    byId("back-dashboard-btn").addEventListener("click", function () {
      if (!confirmDiscardUnsaved()) return;
      window.history.replaceState({}, "", "/workbench");
      api("/workbench/account").then(function (res) {
        if (res.ok) renderAccount(res.data);
        else {
          window.history.replaceState({}, "", "/workbench/login");
          showAuth("login");
        }
      });
    });
    document.addEventListener("keydown", function (event) {
      if (ctx.activeDocumentFrame && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        ctx.activeDocumentFrame.contentWindow.postMessage({ type: "workbench-pdf-find" }, window.location.origin);
      }
    }, true);
    var saved = null;
    try {
      saved = sessionStorage.getItem("workbench.reviewer");
    } catch (exc) {
      saved = null;
    }
    if (saved) byId("reviewer-name").value = saved;
    syncReviewerIdentity();
    byId("reviewer-name").addEventListener("input", function () {
      syncReviewerIdentity();
      try {
        sessionStorage.setItem("workbench.reviewer", reviewer());
      } catch (exc) {
        /* session storage is optional */
      }
    });

    byId("create-project-btn").addEventListener("click", createProject);
    byId("schema-setup-tab").addEventListener("click", function () {
      showSetupTab(byId("schema-setup-panel").hidden ? "schema" : "package");
    });
    byId("use-default-schema-btn").addEventListener("click", useDefaultSchema);
    byId("use-schema-json-btn").addEventListener("click", function () { useSchemaText(byId("field-schema-json").value); });
    byId("field-schema-file").addEventListener("change", function () {
      var file = byId("field-schema-file").files[0];
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) {
        byId("schema-status").textContent = "The field schema exceeds 2 MB.";
        return;
      }
      file.text().then(useSchemaText, function () {
        byId("schema-status").textContent = "Could not read the selected JSON file.";
      });
    });
    byId("tender-file").addEventListener("change", function () {
      var file = byId("tender-file").files[0];
      byId("selected-tender-name").textContent = file ? file.name : "No tender selected";
      byId("create-project-btn").hidden = false;
      byId("create-project-btn").disabled = !file;
      byId("run-understanding-btn").hidden = true;
      if (file && ctx.projectId) {
        ctx.projectId = null;
        ctx.state = null;
        ctx.tenderDocuments = null;
        byId("project-id").value = "";
        byId("run-understanding-btn").disabled = true;
        updateStepper();
        showMessage("New tender selected.", "ok");
      }
    });
    byId("run-understanding-btn").addEventListener("click", runUnderstanding);
    byId("project-id").addEventListener("change", function () {
      ctx.projectId = null;
      ctx.state = null;
      updateStepper();
    });
    byId("load-ledger-btn").addEventListener("click", loadLedger);
    byId("run-compliance-btn").addEventListener("click", runCompliance);
    byId("reload-compliance-btn").addEventListener("click", loadCompliance);
    if (byId("back-to-tender-review-btn")) {
      byId("back-to-tender-review-btn").addEventListener("click", function () {
        window.location.href = pageUrl("ledger");
      });
    }
    if (byId("proceed-to-contract-btn")) {
      byId("proceed-to-contract-btn").addEventListener("click", openChecklist);
    }
    byId("compliance-reupload-btn").addEventListener("click", reuploadTender);
    byId("open-checklist-btn").addEventListener("click", openChecklist);
    byId("back-to-ledger-btn").addEventListener("click", function () {
      if (ctx.draftDirty && !window.confirm("Leave without saving the checklist changes?")) return;
      window.location.href = (complianceInfo() && complianceInfo().enabled)
        ? pageUrl("compliance")
        : pageUrl("ledger");
    });
    byId("load-draft-btn").addEventListener("click", loadDraft);
    byId("add-criterion-btn").addEventListener("click", addChecklistCriterion);
    byId("save-contract-draft-btn").addEventListener("click", saveContractDraft);
    byId("checklist-search").addEventListener("input", function () { if (ctx.draft) renderDraft(); });
    byId("checklist-family-filter").addEventListener("change", function () { if (ctx.draft) renderDraft(); });
    byId("checklist-state-filter").addEventListener("change", function () { if (ctx.draft) renderDraft(); });
    byId("checklist-origin-filter").addEventListener("change", function () { if (ctx.draft) renderDraft(); });
    byId("freeze-contract-btn").addEventListener("click", attemptContinue);
    byId("add-bid-btn").addEventListener("click", addBid);
    byId("run-intake-btn").addEventListener("click", function () { runIntake(); });
    byId("run-evaluate-btn").addEventListener("click", handleEvaluationAction);
    byId("run-evaluate-all-btn").addEventListener("click", function () { runEvaluate(true); });
    byId("save-classification-btn").addEventListener("click", saveClassification);
    byId("approve-classification-btn").addEventListener("click", approveClassification);
    byId("continue-to-evaluation-btn").addEventListener("click", continueToEvaluation);
    byId("propose-classification-btn").addEventListener("click", improveClassification);
    byId("classification-reason").addEventListener("input", updateClassificationState);
    byId("back-to-bidders").addEventListener("click", function () {
      if (!confirmDiscardUnsaved()) return;
      window.location.href = byId("back-to-bidders").getAttribute("data-href") || pageUrl("bids");
    });
    byId("manual-review-vendor").addEventListener("change", function () {
      if (!confirmDiscardUnsaved()) {
        byId("manual-review-vendor").value = ctx.activeClassification ? ctx.activeClassification.vendor : "";
        return;
      }
      var vendor = byId("manual-review-vendor").value;
      window.history.replaceState({}, "", pageUrl("evaluate") + "?vendor=" + encodeURIComponent(vendor));
      loadManualBidReview(vendor);
      updateStepper();
    });
    byId("load-results-btn").addEventListener("click", function () { loadResults(true); });
    byId("load-review-log-btn").addEventListener("click", loadReviewLog);
    byId("select-all-report-vendors").addEventListener("click", function () { selectReportVendors(true); });
    byId("clear-report-vendors").addEventListener("click", function () { selectReportVendors(false); });
    byId("download-xlsx-btn").addEventListener("click", downloadExcel);
    byId("download-json-btn").addEventListener("click", downloadJson);
    byId("download-md-btn").addEventListener("click", downloadMarkdown);
    updateStepper();
    loadDefaultSchemaInfo();
    loadAccount(route);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  Object.assign(ctx, {
    init: init,
  });
}
