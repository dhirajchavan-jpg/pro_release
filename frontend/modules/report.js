"use strict";
/* report downloads and summary. */

export function installReport(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var apiFetch = function () { return ctx.apiFetch.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var criterionKind = function () { return ctx.criterionKind.apply(ctx, arguments); };
  var decisionStats = function () { return ctx.decisionStats.apply(ctx, arguments); };
  var displayReason = function () { return ctx.displayReason.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var renderAwardSuggestion = function () { return ctx.renderAwardSuggestion.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  // ---------------------------------------------------------------- Step 8

  function download(path, filename) {
    return apiFetch(path)
      .then(function (response) {
        if (!response.ok) throw new Error("download failed");
        return response.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var anchor = el("a", { href: url, download: filename });
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
      })
      .catch(function () {
        showMessage("The report download failed.", "error");
      });
  }

  function downloadJson() {
    if (!ctx.projectId) return;
    var vendors = selectedReportVendors();
    if (!vendors.length) return showMessage("Select at least one bidder for the report.", "error");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report").then(function (res) {
      if (!res.ok) return showMessage(apiError(res.data, "Report is not ready yet."), "error");
      var reports = res.data.reports || {};
      var missing = vendors.filter(function (vendor) { return !reports[vendor]; });
      if (missing.length) {
        return showMessage("Reports are not ready for: " + missing.join(", ") + ".", "error");
      }
      var payload = {};
      vendors.forEach(function (vendor) { payload[vendor] = reports[vendor]; });
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var anchor = el("a", {
        href: url,
        download: "bid-evaluation-report-" + ctx.projectId + ".json",
      });
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
      if (res.data.state) {
        ctx.state = res.data.state;
        updateStepper();
      }
    });
  }

  function downloadMarkdown() {
    if (!ctx.projectId) return;
    var vendors = selectedReportVendors();
    if (!vendors.length) return showMessage("Select at least one bidder for the report.", "error");
    var params = new URLSearchParams();
    vendors.forEach(function (vendor) { params.append("vendor", vendor); });
    download(
      "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report/markdown?" + params.toString(),
      "bid-evaluation-report-" + ctx.projectId + ".md"
    );
  }

  function selectedReportVendors() {
    return Array.from(byId("report-vendors").querySelectorAll("input:checked")).map(function (input) { return input.value; });
  }

  function selectReportVendors(checked) {
    byId("report-vendors").querySelectorAll("input").forEach(function (input) { input.checked = checked; });
    renderReportSummary();
  }

  function reportCriterionName(criterion) {
    var requirement = String(criterion.requirement || criterion.category || "Requirement").trim();
    var colon = requirement.indexOf(":");
    if (colon > 0 && colon < 80) requirement = requirement.slice(0, colon);
    return requirement.length > 100 ? requirement.slice(0, 97).trim() + "…" : requirement;
  }

  function reportReason(criterion) {
    var reason = displayReason(criterion.why) || "No conclusive reason was recorded.";
    return reason.length > 240 ? reason.slice(0, 237).trim() + "…" : reason;
  }

  function reportFindingGroup(title, criteria, kind) {
    var group = el("section", { className: "report-finding-group " + kind });
    group.appendChild(el("h4", { text: title + " (" + criteria.length + ")" }));
    if (!criteria.length) {
      group.appendChild(el("p", { className: "report-none", text: "None" }));
      return group;
    }
    var list = el("ul");
    criteria.forEach(function (criterion) {
      var item = el("li", {}, [el("strong", { text: reportCriterionName(criterion) })]);
      if (kind !== "pass") {
        item.appendChild(el("p", { text: reportReason(criterion) }));
      }
      list.appendChild(item);
    });
    group.appendChild(list);
    return group;
  }

  function renderReportSummary() {
    var host = byId("report-summary");
    host.textContent = "";
    if (!ctx.reportData || !Object.keys(ctx.reportData).length) {
      host.appendChild(el("div", { className: "result-empty", text: "Loading report results…" }));
      return;
    }
    var suggestion = renderAwardSuggestion(ctx.reportAward);
    if (suggestion) host.appendChild(suggestion);
    var vendors = selectedReportVendors();
    if (!vendors.length) {
      host.appendChild(el("div", { className: "result-empty", text: "Select at least one bidder to view the report." }));
      return;
    }
    if (!ctx.reportStatus.complete) {
      host.appendChild(el("div", {
        className: "result-empty",
        text: "Partial report: evaluation is still pending for " + ctx.reportStatus.missing_vendors.join(", ") + ".",
      }));
    }
    var comparisonWrap = el("div", { className: "matrix-wrap" });
    var comparison = el("table", { className: "result-table" });
    comparison.appendChild(el("tr", {}, [
      el("th", { text: "Bidder" }),
      el("th", { text: "Outcome" }),
      el("th", { text: "Passed" }),
      el("th", { text: "Failed / missing" }),
      el("th", { text: "Review" }),
      el("th", { text: "Not evaluated" }),
    ]));
    vendors.forEach(function (vendor) {
      var report = ctx.reportData[vendor];
      if (!report) return;
      var summary = report.decision_summary || {};
      comparison.appendChild(el("tr", {}, [
        el("td", { text: ((report.vendor || {}).vendor_name || (report.vendor || {}).name || vendor) }),
        el("td", {}, [pill(summary.label || "Review", summary.outcome || "review")]),
        el("td", { text: String(summary.passed || 0) }),
        el("td", { text: String(summary.failed || 0) }),
        el("td", { text: String(summary.review || 0) }),
        el("td", { text: String(summary.not_evaluated || 0) }),
      ]));
    });
    comparisonWrap.appendChild(comparison);
    host.appendChild(comparisonWrap);
    vendors.forEach(function (vendor) {
      var report = ctx.reportData[vendor];
      if (!report) return;
      var summary = report.decision_summary || {};
      var criteria = report.criteria_results || [];
      var findings = { pass: [], fail: [], review: [], not_evaluated: [] };
      criteria.forEach(function (criterion) {
        var bucket = criterion.evaluation_state === "not_evaluated_after_blocker" || criterion.status === "not_evaluated"
          ? "not_evaluated" : criterionKind(criterion);
        findings[bucket].push(criterion);
      });
      var card = el("section", { className: "report-result" });
      var bidderName = (report.vendor || {}).vendor_name || (report.vendor || {}).name || vendor;
      card.appendChild(el("div", { className: "report-decision-header" }, [
        el("div", {}, [
          el("span", { className: "decision-label", text: "Bidder decision" }),
          el("h3", { text: bidderName }),
          el("p", {
            text: String(summary.passed || 0) + " of " + String(summary.total || criteria.length) +
              " evaluated requirements passed.",
          }),
        ]),
        pill(summary.label || report.final_decision || "Review", summary.outcome || "review"),
      ]));
      card.appendChild(decisionStats(summary));
      var findingGroups = [];
      if (findings.fail.length) findingGroups.push(reportFindingGroup("Failed or missing", findings.fail, "fail"));
      if (findings.review.length) findingGroups.push(reportFindingGroup("Needs review", findings.review, "review"));
      if (findings.not_evaluated.length) findingGroups.push(reportFindingGroup("Not evaluated", findings.not_evaluated, "not-evaluated"));
      if (findings.pass.length) findingGroups.push(reportFindingGroup("Passed", findings.pass, "pass"));
      card.appendChild(el("div", { className: "report-findings" }, findingGroups));
      var fullEvaluation = el("button", { className: "secondary", type: "button", text: "View full evaluation" });
      fullEvaluation.addEventListener("click", function () {
        window.location.href = pageUrl("results") + "?vendor=" + encodeURIComponent(vendor);
      });
      card.appendChild(el("div", { className: "report-card-actions" }, [fullEvaluation]));
      host.appendChild(card);
    });
  }

  function loadReport() {
    var force = arguments[0] === true;
    if (!ctx.projectId || (!force && ctx.reportLoadedProject === ctx.projectId)) return Promise.resolve();
    var finalize = ctx.state && ctx.state.stage === "evaluated"
      ? api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report/generate", { method: "POST" })
      : Promise.resolve({ ok: false, data: {} });
    return finalize.then(function (generated) {
      if (generated.ok && generated.data.state) {
        ctx.state = generated.data.state;
        updateStepper();
      }
      if (generated.ok && generated.data.reports) return generated;
      return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report");
    }).then(function (res) {
      if (!res.ok) {
        byId("report-summary").textContent = apiError(res.data, "The report is not ready yet.");
        return;
      }
      if (res.data.state) {
        ctx.state = res.data.state;
        updateStepper();
      }
      ctx.reportData = res.data.reports || {};
      ctx.reportAward = res.data.award_recommendation || null;
      ctx.reportStatus = {
        complete: res.data.complete !== false,
        missing_vendors: res.data.missing_vendors || [],
      };
      renderReportSummary();
      ctx.reportLoadedProject = ctx.projectId;
    });
  }

  function downloadExcel() {
    if (!ctx.projectId) return;
    var vendors = selectedReportVendors();
    if (!vendors.length) return showMessage("Select at least one bidder for the Excel report.", "error");
    var params = new URLSearchParams();
    vendors.forEach(function (vendor) { params.append("vendor", vendor); });
    download(
      "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report/excel?" + params.toString(),
      "bid-evaluation-report-" + ctx.projectId + ".xlsx"
    );
  }


  Object.assign(ctx, {
    download: download,
    downloadJson: downloadJson,
    downloadMarkdown: downloadMarkdown,
    selectedReportVendors: selectedReportVendors,
    selectReportVendors: selectReportVendors,
    reportCriterionName: reportCriterionName,
    reportReason: reportReason,
    reportFindingGroup: reportFindingGroup,
    renderReportSummary: renderReportSummary,
    loadReport: loadReport,
    downloadExcel: downloadExcel,
  });
}

