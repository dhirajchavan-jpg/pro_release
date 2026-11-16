"use strict";
/* account and portfolio screens. */

export function installAccount(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var accountMessage = function () { return ctx.accountMessage.apply(ctx, arguments); };
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var applyPageMode = function () { return ctx.applyPageMode.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var criterionKind = function () { return ctx.criterionKind.apply(ctx, arguments); };
  var criterionLabel = function () { return ctx.criterionLabel.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var friendlyStatus = function () { return ctx.friendlyStatus.apply(ctx, arguments); };
  var hideRouteLoading = function () { return ctx.hideRouteLoading.apply(ctx, arguments); };
  var operationIsActive = function () { return ctx.operationIsActive.apply(ctx, arguments); };
  var operationIsReady = function () { return ctx.operationIsReady.apply(ctx, arguments); };
  var operationStatus = function () { return ctx.operationStatus.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var resumeProject = function () { return ctx.resumeProject.apply(ctx, arguments); };
  var showAuth = function () { return ctx.showAuth.apply(ctx, arguments); };
  var showRouteLoading = function () { return ctx.showRouteLoading.apply(ctx, arguments); };
  var syncReviewerIdentity = function () { return ctx.syncReviewerIdentity.apply(ctx, arguments); };
  function showWorkflow(page, keepLoading) {
    byId("account-shell").hidden = true;
    byId("global-header").hidden = false;
    byId("app-footer").hidden = false;
    byId("workflow-header").hidden = false;
    byId("workflow-main").hidden = false;
    applyPageMode(page || "tender");
    if (!keepLoading) hideRouteLoading();
  }

  function showProjectContext(project) {
    byId("workflow-tender-title").textContent = project && project.title
      ? project.title : project ? "Untitled tender" : "New tender";
    byId("workflow-tender-number").textContent = project
      ? (project.tender_number || project.project_id || "") : "Not yet created";
    var auditAvailable = project
      && ctx.STAGES.indexOf(project.stage) >= ctx.STAGES.indexOf("evaluated");
    byId("footer-audit-link").hidden = !auditAvailable;
    if (project) {
      byId("footer-audit-link").href = "/workbench/ui/" + encodeURIComponent(project.project_id) + "/report#review-log-panel";
    }
  }

  function projectPage(project) {
    var index = ctx.STAGES.indexOf(project.stage);
    var page = ctx.PAGE_NAMES[Math.max(0, Math.min(index, ctx.PAGE_NAMES.length - 1))];
    if (page === "compliance" && !(project.compliance && project.compliance.enabled)) {
      page = "ledger";
    }
    return page;
  }

  function projectStageLabel(stage) {
    return {
      tender_uploaded: "Tender uploaded",
      rfp_understood: "Tender ready for review",
      contract_drafted: "Review in progress",
      contract_frozen: "Tender reviewed",
      bids_intaken: "Ready to evaluate",
      evaluated: "Results ready",
      report_generated: "Report ready",
    }[stage] || friendlyStatus(stage);
  }

  function renderPortfolio() {
    var query = byId("portfolio-search").value.trim().toLowerCase();
    var status = byId("portfolio-status").value;
    var rows = ctx.portfolioProjects.filter(function (project) {
      var summary = project.portfolio_summary || {};
      var searchable = [project.tender_number, project.title].join(" ").toLowerCase();
      return (!query || searchable.indexOf(query) !== -1) && (!status || summary.status === status);
    });
    var body = byId("portfolio-table-body");
    body.replaceChildren();
    rows.forEach(function (project) {
      var summary = project.portfolio_summary || {};
      var projectStatus = summary.status || "In progress";
      var statusKind = projectStatus === "Complete" ? "pass" :
        projectStatus === "Needs attention" ? "review" : "review_note";
      var hasBidders = Number(summary.bidder_count || 0) > 0;
      var action = el("button", {
        className: "secondary portfolio-action",
        type: "button",
        text: hasBidders ? "Compare bidders" : "Continue tender",
      });
      action.addEventListener("click", function () {
        window.location.href = hasBidders
          ? "/workbench?view=portfolio&tender=" + encodeURIComponent(project.project_id)
          : "/workbench/ui/" + encodeURIComponent(project.project_id) + "/" + projectPage(project);
      });
      body.appendChild(el("tr", {}, [
        el("td", {}, [
          el("strong", { text: project.title || "Untitled tender" }),
          el("small", { text: project.tender_number || project.project_id }),
        ]),
        el("td", { text: projectStageLabel(project.stage) }),
        el("td", { text: String(summary.bidder_count || 0) }),
        el("td", { text: String(summary.qualified_count || 0) }),
        el("td", { text: String(summary.failed_count || 0) }),
        el("td", { text: String(summary.review_count || 0) }),
        el("td", {}, [pill(projectStatus, statusKind)]),
        el("td", {}, [action]),
      ]));
    });
    byId("portfolio-empty").hidden = rows.length !== 0;
  }

  function bidderStatusKind(value) {
    return value === "Qualified" || value === "Pass" ? "pass" :
      value === "Not qualified" || value === "Fail" ? "fail" : "review";
  }

  function effectiveCheckKind(check) {
    if (!check) return "review";
    var decision = String(((check.human_review || {}).decision) || "");
    if (decision === "meets_requirement") return "pass";
    if (decision === "does_not_meet") return "fail";
    return criterionKind(check);
  }

  function comparisonBidder(vendorId, item) {
    var summary = item.decision_summary || {};
    var status = operationStatus(item);
    var complete = operationIsReady(item);
    var outcome = status === "partial" ? "Needs review" : complete ? (summary.label || "Needs review") :
      operationIsActive(item) ? (status === "retrying" ? "Retrying" : "In progress") :
      status === "failed" ? "Failed" : "Not started";
    var criteria = {};
    (item.criteria || []).forEach(function (group) {
      (group.checks || [group]).forEach(function (check) {
        var key = String(check.criterion_id || group.criterion_id || check.checkpoint_id || "");
        if (key) criteria[key] = check;
      });
    });
    return {
      id: vendorId,
      name: item.vendor_name || vendorId,
      checks: Number(summary.total || Object.keys(criteria).length),
      passed: Number(summary.passed || 0),
      failed: Number(summary.failed || 0),
      review: Number(summary.review || 0) + Number(summary.not_evaluated || 0),
      finding: (summary.key_failures || [])[0] ||
        (summary.key_reviews || [])[0] ||
        (summary.key_passes || [])[0] ||
        (complete ? "Evaluation completed" : "Evaluation has not completed"),
      outcome: outcome,
      criteria: criteria,
      complete: complete,
    };
  }

  function renderBidderComparison() {
    if (!ctx.portfolioResult || !ctx.portfolioProject) return;
    var query = byId("bidder-search").value.trim().toLowerCase();
    var status = byId("bidder-status").value;
    var allBidders = Object.keys(ctx.portfolioResult.vendors || {}).map(function (vendorId) {
      return comparisonBidder(vendorId, ctx.portfolioResult.vendors[vendorId]);
    });
    var bidders = allBidders.filter(function (bidder) {
      return (!query || bidder.name.toLowerCase().indexOf(query) !== -1) &&
        (!status || bidder.outcome === status);
    });
    var body = byId("bidder-table-body");
    body.replaceChildren();
    bidders.forEach(function (bidder) {
      var action = el("button", { className: "secondary portfolio-action", type: "button", text: "Open result" });
      action.addEventListener("click", function () {
        window.location.href = "/workbench/ui/" + encodeURIComponent(ctx.portfolioProject.project_id) +
          "/" + (bidder.complete ? "results" : "evaluate") +
          (bidder.complete ? "?vendor=" + encodeURIComponent(bidder.id) : "");
      });
      body.appendChild(el("tr", {}, [
        el("td", {}, [el("strong", { text: bidder.name }), el("small", { text: "Technical bid" })]),
        el("td", { text: String(bidder.checks) }),
        el("td", { text: String(bidder.passed) }),
        el("td", { text: String(bidder.failed) }),
        el("td", { text: String(bidder.review) }),
        el("td", { text: bidder.finding }),
        el("td", {}, [pill(bidder.outcome, bidderStatusKind(bidder.outcome))]),
        el("td", {}, [action]),
      ]));
    });
    byId("bidder-empty").hidden = bidders.length !== 0;

    var matrix = byId("criteria-matrix");
    matrix.replaceChildren();
    var head = el("tr", {}, [el("th", { text: "Tender requirement" })]);
    allBidders.forEach(function (bidder) {
      head.appendChild(el("th", { text: bidder.name }));
    });
    matrix.appendChild(el("thead", {}, [head]));
    var matrixBody = el("tbody");
    var criteria = [];
    allBidders.forEach(function (bidder) {
      Object.keys(bidder.criteria).forEach(function (key) {
        if (!criteria.some(function (criterion) { return criterion.key === key; })) {
          var check = bidder.criteria[key];
          criteria.push({ key: key, title: check.title || check.requirement || key });
        }
      });
    });
    criteria.forEach(function (criterion) {
      var row = el("tr", {}, [el("td", { text: criterion.title })]);
      allBidders.forEach(function (bidder) {
        var check = bidder.criteria[criterion.key];
        var kind = check ? effectiveCheckKind(check) : "review";
        var value = check ? criterionLabel(kind, check) : "Not evaluated";
        row.appendChild(el("td", {}, [pill(value, kind)]));
      });
      matrixBody.appendChild(row);
    });
    matrix.appendChild(matrixBody);
    if (!criteria.length) {
      matrixBody.appendChild(el("tr", {}, [
        el("td", {
          colspan: String(1 + allBidders.length),
          text: allBidders.length ? "No completed criterion results are available yet." : "No bidders have been submitted.",
        }),
      ]));
    }
  }

  function loadBidderComparison(project) {
    ctx.portfolioProject = project;
    ctx.portfolioResult = null;
    byId("comparison-tender-number").textContent = project.tender_number || project.project_id;
    byId("comparison-tender-title").textContent = project.title || "Untitled tender";
    byId("comparison-evaluation-status").textContent = "Loading evaluation";
    byId("bidder-table-body").replaceChildren();
    byId("criteria-matrix").replaceChildren();
    api("/workbench/projects/" + encodeURIComponent(project.project_id) + "/results").then(function (res) {
      if (!res.ok) {
        byId("comparison-evaluation-status").textContent = "Results unavailable";
        byId("comparison-evaluation-note").textContent = apiError(res.data, "Could not load bidder results.");
        return accountMessage(apiError(res.data, "Could not load bidder results."), "error");
      }
      ctx.portfolioResult = res.data;
      var comparison = res.data.comparison || {};
      var outcomes = comparison.outcomes || {};
      byId("comparison-bidder-count").textContent = String(comparison.bidder_count || 0);
      byId("comparison-qualified-count").textContent = String(outcomes.pass || 0);
      byId("comparison-failed-count").textContent = String(outcomes.fail || 0);
      byId("comparison-review-count").textContent = String(outcomes.review || 0);
      byId("comparison-evaluation-status").textContent = res.data.complete
        ? "Committee review ready"
        : String(comparison.evaluated_count || 0) + " of " + String(comparison.bidder_count || 0) + " evaluated";
      byId("comparison-evaluation-note").textContent = res.data.complete
        ? "Commercial bids remain restricted"
        : String(comparison.pending_count || 0) + " bidder evaluation(s) pending";
      byId("bidder-report-btn").disabled = Number(comparison.evaluated_count || 0) === 0;
      renderBidderComparison();
    });
  }

  function renderAccount(data) {
    var params = new URLSearchParams(window.location.search);
    var portfolioMode = params.get("view") === "portfolio" || params.get("portfolio") === "demo";
    var selectedTenderId = params.get("tender");
    var tenderComparisonMode = portfolioMode && Boolean(selectedTenderId);
    ctx.portfolioProjects = data.projects || [];
    ctx.portfolioProject = selectedTenderId
      ? ctx.portfolioProjects.find(function (project) { return project.project_id === selectedTenderId; })
      : null;
    if (tenderComparisonMode && !ctx.portfolioProject) {
      window.history.replaceState({}, "", "/workbench?view=portfolio");
      tenderComparisonMode = false;
      accountMessage("That tender is not available in this workspace.", "error");
    }
    byId("account-shell").classList.remove("auth-mode");
    byId("account-shell").classList.add("dashboard-mode");
    byId("account-shell").hidden = false;
    byId("auth-panel").hidden = true;
    byId("dashboard").hidden = false;
    byId("global-header").hidden = false;
    byId("app-footer").hidden = false;
    byId("footer-audit-link").hidden = true;
    byId("workflow-header").hidden = true;
    byId("workflow-main").hidden = true;
    hideRouteLoading();
    byId("profile-name").value = data.user.name || "";
    byId("profile-phone").value = data.user.phone || "";
    byId("reviewer-name").value = byId("reviewer-name").value || data.user.name || "Local procurement operator";
    syncReviewerIdentity();
    byId("dashboard-title").textContent = tenderComparisonMode ? "Bidder comparison" :
      portfolioMode ? "Procurement portfolio" : data.user.name + "'s tenders";
    byId("portfolio-toggle-btn").textContent = tenderComparisonMode ? "Portfolio overview" :
      portfolioMode ? "My tenders" : "Portfolio overview";
    byId("portfolio-overview").hidden = !portfolioMode || tenderComparisonMode;
    byId("bidder-comparison").hidden = !tenderComparisonMode;
    byId("profile-settings").hidden = portfolioMode;
    byId("tender-list-head").hidden = portfolioMode;
    byId("project-list").hidden = portfolioMode;
    if (tenderComparisonMode) {
      loadBidderComparison(ctx.portfolioProject);
    } else if (portfolioMode) {
      var statuses = ctx.portfolioProjects.map(function (project) {
        return (project.portfolio_summary || {}).status || "In progress";
      });
      byId("portfolio-active-count").textContent = String(ctx.portfolioProjects.length);
      byId("portfolio-complete-count").textContent = String(statuses.filter(function (value) { return value === "Complete"; }).length);
      byId("portfolio-progress-count").textContent = String(statuses.filter(function (value) { return value === "In progress"; }).length);
      byId("portfolio-attention-count").textContent = String(statuses.filter(function (value) { return value === "Needs attention"; }).length);
      var updated = ctx.portfolioProjects.map(function (project) { return project.updated_at || ""; }).sort().pop();
      byId("portfolio-updated").textContent = updated
        ? "Updated " + new Date(updated).toLocaleString()
        : "No tenders have been created";
      renderPortfolio();
    }
    byId("tender-count").textContent = data.projects.length + (data.projects.length === 1 ? " tender" : " tenders");
    var list = byId("project-list");
    list.replaceChildren();
    if (!data.projects.length) {
      list.appendChild(el("p", { text: "No tenders yet. Select New tender to begin." }));
      return;
    }
    data.projects.forEach(function (project) {
      var number = el("input", { type: "text", value: project.tender_number || project.project_id, "aria-label": "Tender number" });
      var title = el("input", { type: "text", value: project.title || "", placeholder: "Tender title", "aria-label": "Tender title" });
      var numberText = el("div", { className: "project-number", text: project.tender_number || project.project_id });
      var titleText = el("h3", { text: project.title || "Untitled tender" });
      var edit = el("button", { className: "secondary", type: "button", text: "Edit" });
      var save = el("button", { type: "button", text: "Save" });
      var cancel = el("button", { className: "secondary", type: "button", text: "Cancel" });
      var resume = el("button", { type: "button", text: "Continue" });
      var editForm = el("div", { className: "project-edit" });
      editForm.hidden = true;
      editForm.appendChild(el("div", { className: "field" }, [el("label", { text: "Tender number" }), number]));
      editForm.appendChild(el("div", { className: "field" }, [el("label", { text: "Tender title" }), title]));
      editForm.appendChild(el("div", { className: "dashboard-actions" }, [save, cancel]));
      edit.addEventListener("click", function () {
        editForm.hidden = false;
        edit.hidden = true;
        number.focus();
      });
      cancel.addEventListener("click", function () {
        number.value = project.tender_number || project.project_id;
        title.value = project.title || "";
        editForm.hidden = true;
        edit.hidden = false;
      });
      save.addEventListener("click", function () {
        api("/workbench/account/projects/" + encodeURIComponent(project.project_id), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_number: number.value.trim(), title: title.value.trim() }),
        }).then(function (res) {
          if (!res.ok) return accountMessage(apiError(res.data, "Could not save tender details."), "error");
          project.tender_number = number.value.trim();
          project.title = title.value.trim();
          numberText.textContent = project.tender_number || project.project_id;
          titleText.textContent = project.title || "Untitled tender";
          editForm.hidden = true;
          edit.hidden = false;
          accountMessage("Tender details saved.", "ok");
        });
      });
      resume.addEventListener("click", function () {
        window.location.href = "/workbench/ui/" + encodeURIComponent(project.project_id) + "/" + projectPage(project);
      });
      var actions = el("div", { className: "dashboard-actions" }, [edit, resume]);
      list.appendChild(el("div", {
        className: "project-card",
        "data-project-id": project.project_id,
      }, [
        el("div", {}, [numberText, el("div", { className: "meta", text: "Project " + project.project_id })]),
        el("div", {}, [titleText]),
        el("div", { className: "project-status" }, [pill(projectStageLabel(project.stage), project.stage === "report_generated" ? "pass" : "review")]),
        actions,
        editForm,
      ]));
    });
  }

  function submitAccount(path, payload) {
    accountMessage("Please wait…");
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) return accountMessage(apiError(res.data, "The request could not be processed."), "error");
      accountMessage("");
      window.history.replaceState({}, "", "/workbench");
      renderAccount(res.data);
      if (ctx.loadDefaultSchemaInfo) ctx.loadDefaultSchemaInfo();
    });
  }

  function loadAccount(route, attempt) {
    attempt = attempt || 0;
    api("/workbench/account" + (route ? "?include_projects=false" : "")).then(function (res) {
      if (!res.ok) {
        if (res.status !== 401 && res.status !== 403) {
          showRouteLoading("Reconnecting to service…");
          window.setTimeout(function () { loadAccount(route, attempt + 1); }, Math.min(5000, 1000 * (attempt + 1)));
          return;
        }
        var mode = window.location.pathname === "/workbench/register" ? "register" : "login";
        if (window.location.pathname === "/workbench") window.history.replaceState({}, "", "/workbench/login");
        return showAuth(mode);
      }
      if (!route) return renderAccount(res.data);
      byId("reviewer-name").value = byId("reviewer-name").value || res.data.user.name;
      syncReviewerIdentity();
      showProjectContext(res.data.projects.find(function (project) {
        return project.project_id === route.projectId;
      }) || { project_id: route.projectId });
      showWorkflow(route.page, true);
      ctx.projectId = route.projectId;
      byId("project-id").value = ctx.projectId;
      resumeProject();
    });
  }

  Object.assign(ctx, {
    showWorkflow: showWorkflow,
    showProjectContext: showProjectContext,
    projectPage: projectPage,
    projectStageLabel: projectStageLabel,
    renderPortfolio: renderPortfolio,
    bidderStatusKind: bidderStatusKind,
    effectiveCheckKind: effectiveCheckKind,
    comparisonBidder: comparisonBidder,
    renderBidderComparison: renderBidderComparison,
    loadBidderComparison: loadBidderComparison,
    renderAccount: renderAccount,
    submitAccount: submitAccount,
    loadAccount: loadAccount,
  });
}
