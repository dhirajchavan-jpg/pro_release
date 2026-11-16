"use strict";
/* route and workflow navigation. */

export function installNavigation(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiFetch = function () { return ctx.apiFetch.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var classificationChanged = function () { return ctx.classificationChanged.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var hideRouteLoading = function () { return ctx.hideRouteLoading.apply(ctx, arguments); };
  var loadCompliance = function () { return ctx.loadCompliance.apply(ctx, arguments); };
  var loadDraft = function () { return ctx.loadDraft.apply(ctx, arguments); };
  var loadLedger = function () { return ctx.loadLedger.apply(ctx, arguments); };
  var loadReport = function () { return ctx.loadReport.apply(ctx, arguments); };
  var loadResults = function () { return ctx.loadResults.apply(ctx, arguments); };
  var loadReviewLog = function () { return ctx.loadReviewLog.apply(ctx, arguments); };
  var manualReviewVendor = function () { return ctx.manualReviewVendor.apply(ctx, arguments); };
  var operationErrorText = function () { return ctx.operationErrorText.apply(ctx, arguments); };
  var operationIsActive = function () { return ctx.operationIsActive.apply(ctx, arguments); };
  var operationIsReady = function () { return ctx.operationIsReady.apply(ctx, arguments); };
  var operationStatus = function () { return ctx.operationStatus.apply(ctx, arguments); };
  var prepareManualReview = function () { return ctx.prepareManualReview.apply(ctx, arguments); };
  var renderBidderRoster = function () { return ctx.renderBidderRoster.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };
  var showRouteLoading = function () { return ctx.showRouteLoading.apply(ctx, arguments); };

  function stageIndex() {
    return ctx.state ? ctx.STAGES.indexOf(ctx.state.stage) : -1;
  }

  function complianceInfo() {
    return ctx.state && ctx.state.compliance ? ctx.state.compliance : null;
  }

  function complianceDone() {
    var info = complianceInfo();
    if (!info || !info.enabled) return stageIndex() >= ctx.STAGES.indexOf("rfp_understood");
    return info.status === "completed" && !(info.open > 0);
  }

  function complianceBlocksContract() {
    var info = complianceInfo();
    return Boolean(info && info.enabled && (info.status !== "completed" || (info.open || 0) > 0));
  }

  function manualReviewOnly() {
    return Boolean(
      ctx.state &&
      ctx.state.artifacts.indexOf("contract_limitations") !== -1 &&
      ctx.state.artifacts.indexOf("evaluation_plan") === -1
    );
  }

  function bidderIntakeComplete(vendor) {
    return Boolean(
      ctx.state && (
        ctx.state.artifacts.indexOf("manual_intake:" + vendor) !== -1 ||
        ctx.state.artifacts.some(function (name) { return name.indexOf("submission:" + vendor + ":") === 0; })
      )
    );
  }

  function bidderRecord(vendor) {
    return (ctx.state && Array.isArray(ctx.state.bidders) ? ctx.state.bidders : []).find(function (bidder) {
      return bidder.vendor_id === vendor;
    }) || {};
  }

  function bidderClassificationApproved(vendor) {
    return bidderRecord(vendor).classification_status === "approved";
  }

  function bidderDocumentsPrepared(vendor) {
    return Boolean(bidderRecord(vendor).documents_prepared);
  }

  function pendingBidderClassifications() {
    return ctx.state ? ctx.state.vendors.filter(function (vendor) {
      return bidderDocumentsPrepared(vendor) && !bidderClassificationApproved(vendor);
    }) : [];
  }

  function updateClassificationContinue() {
    var button = byId("continue-to-evaluation-btn");
    var status = byId("classification-continue-status");
    if (!button || !status) return;
    var pending = pendingBidderClassifications();
    var total = ctx.state ? ctx.state.vendors.filter(bidderDocumentsPrepared).length : 0;
    var alreadyPrepared = stageIndex() >= ctx.STAGES.indexOf("bids_intaken");
    button.hidden = alreadyPrepared;
    button.disabled = alreadyPrepared || !total || pending.length > 0 || classificationChanged();
    status.textContent = alreadyPrepared
      ? "Bidder facts are prepared for evaluation."
      : pending.length
        ? "Review and approve " + pending.length + " remaining bidder classification" +
          (pending.length === 1 ? "" : "s") + " before evaluation."
        : total
          ? (total === 1
            ? "The bidder classification is approved. Run evaluation when ready."
            : "All " + total + " bidder classifications are approved. Run evaluation when ready.")
          : "Prepare bidder documents before evaluation.";
  }

  function bidderMetadata() {
    if (!ctx.projectId) return {};
    var metadata = {};
    try {
      metadata = JSON.parse(localStorage.getItem("workbench.bidders." + ctx.projectId) || "{}");
    } catch (_error) {
      metadata = {};
    }
    (ctx.state && Array.isArray(ctx.state.bidders) ? ctx.state.bidders : []).forEach(function (bidder) {
      metadata[bidder.vendor_id] = {
        displayName: bidder.bidder_name,
        fileName: bidder.original_filename,
        packageBytes: bidder.package_bytes,
        documentsPrepared: bidder.documents_prepared,
        classificationStatus: bidder.classification_status,
      };
    });
    return metadata;
  }

  function saveBidderMetadata(vendor, displayName, fileName) {
    var metadata = bidderMetadata();
    metadata[vendor] = { displayName: displayName, fileName: fileName };
    try {
      localStorage.setItem("workbench.bidders." + ctx.projectId, JSON.stringify(metadata));
    } catch (_error) {
      // Browser storage is optional; the server slug remains the fallback label.
    }
  }

  function bidderSlug(name) {
    var slug = name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
    return slug || ("bidder-" + Date.now().toString(36));
  }

  function routeProject() {
    var match = window.location.pathname.match(/^\/workbench\/ui\/([^/]+)\/(tender|ledger|compliance|contract|bids|evaluate|results|report)$/);
    return match ? { projectId: decodeURIComponent(match[1]), page: match[2] } : null;
  }

  function pageUrl(page) {
    return ctx.projectId
      ? "/workbench/ui/" + encodeURIComponent(ctx.projectId) + "/" + page
      : "/workbench";
  }

  function recordStepView(page) {
    if (!ctx.projectId || ctx.PAGE_NAMES.indexOf(page) === -1) return;
    var route = ctx.projectId + ":" + page;
    if (route === ctx.lastUsageRoute) return;
    var previous = ctx.lastUsageRoute.indexOf(ctx.projectId + ":") === 0
      ? ctx.lastUsageRoute.slice(ctx.projectId.length + 1)
      : null;
    ctx.lastUsageRoute = route;
    apiFetch("/v1/workbench/usage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: ctx.projectId, step: page, previous_step: previous }),
      keepalive: true,
    }).catch(function () {});
  }

  function loadActivePage(page) {
    if (!ctx.projectId || !ctx.state) return Promise.resolve();
    if (page === "ledger") return loadLedger();
    if (page === "compliance") return loadCompliance();
    if (page === "contract") return loadDraft();
    if (page === "evaluate") return prepareManualReview();
    if (page === "results") return loadResults();
    if (page === "report") {
      loadReviewLog();
      return loadReport();
    }
    return Promise.resolve();
  }

  function navigateWorkflow(page, url, pushHistory) {
    if (ctx.PAGE_NAMES.indexOf(page) === -1 || !ctx.projectId) return;
    if (page === "compliance" && ctx.state && !(ctx.state.compliance && ctx.state.compliance.enabled)) {
      page = "ledger";
      url = pageUrl(page);
    }
    showRouteLoading("Loading " + byId("pill-" + (ctx.PAGE_NAMES.indexOf(page) + 1)).lastElementChild.textContent.toLowerCase());
    if (pushHistory) window.history.pushState({}, "", url || pageUrl(page));
    applyPageMode(page);
    updateStepper();
    recordStepView(page);
    window.requestAnimationFrame(function () {
      Promise.resolve(loadActivePage(page)).catch(function (errorValue) {
        showMessage(errorValue.message || "This page could not be loaded.", "error");
      }).finally(hideRouteLoading);
    });
  }

  function applyPageMode(page) {
    ctx.activePage = ctx.PAGE_NAMES.indexOf(page) === -1 ? "tender" : page;
    ctx.PAGE_NAMES.forEach(function (name, index) {
      byId("step-" + (index + 1)).hidden = name !== ctx.activePage;
    });
    byId("review-log-panel").hidden = ctx.activePage !== "report";
  }

  function recommendedPage() {
    var page = ctx.PAGE_NAMES[Math.max(0, Math.min(stageIndex(), ctx.PAGE_NAMES.length - 1))];
    if (page === "compliance" && !(ctx.state && ctx.state.compliance && ctx.state.compliance.enabled)) {
      page = "ledger";
    }
    return page;
  }

  function pill(text, kind) {
    return el("span", { className: "pill " + kind, text: text });
  }

  function updateStepper() {
    var idx = stageIndex();
    var bidderRows = ctx.state ? (ctx.state.bidders || []) : [];
    var anyEvaluationStarted = bidderRows.some(function (bidder) {
      return Boolean(bidder.evaluation_status);
    });
    var anyEvaluationComplete = bidderRows.some(function (bidder) {
      return operationIsReady({ status: bidder.evaluation_status });
    });
    for (var step = 1; step <= 8; step += 1) {
      var pillNode = byId("pill-" + step);
      var section = byId("step-" + step);
      pillNode.hidden = step === 3 && !(complianceInfo() && complianceInfo().enabled);
      var required = ctx.STEP_REQUIREMENTS[step];
      var unlocked = step === 1 || (idx >= 0 && idx >= required);
      if (step === 6 && manualReviewOnly()) unlocked = ctx.state.vendors.some(bidderIntakeComplete);
      if (step === 6 && ctx.state && ctx.state.vendors.some(bidderDocumentsPrepared)) unlocked = true;
      if (step === 7 && anyEvaluationStarted) unlocked = true;
      if (step === 8 && anyEvaluationComplete) unlocked = true;
      var done = step === 1 ? idx >= 1 : idx > required;
      if (step === 2) done = idx >= ctx.STAGES.indexOf("contract_drafted");
      if (step === 3) done = complianceDone();
      if (step === 4) done = idx >= ctx.STAGES.indexOf("contract_frozen");
      pillNode.className = "step-pill" + (done ? " done" : unlocked ? " available" : "") +
        (ctx.PAGE_NAMES[step - 1] === ctx.activePage ? " current" : "");
      pillNode.href = unlocked ? pageUrl(ctx.PAGE_NAMES[step - 1]) : pageUrl(recommendedPage());
      pillNode.setAttribute("aria-disabled", unlocked ? "false" : "true");
      if (ctx.PAGE_NAMES[step - 1] === ctx.activePage) pillNode.setAttribute("aria-current", "step");
      else pillNode.removeAttribute("aria-current");
      section.classList.toggle("locked", !unlocked);
    }
    var frozen = idx >= ctx.STAGES.indexOf("contract_frozen");
    var pendingManualIntake = ctx.state && ctx.state.vendors.some(function (vendor) {
      return !bidderIntakeComplete(vendor);
    });
    var pendingClassification = ctx.state && ctx.state.vendors.some(function (vendor) {
      return bidderDocumentsPrepared(vendor) && !bidderClassificationApproved(vendor);
    });
    var classificationReviewActive = Boolean(
      ctx.state && ctx.state.vendors.some(bidderDocumentsPrepared) && pendingClassification
    );
    var evaluationComplete = idx >= ctx.STAGES.indexOf("evaluated");
    var evaluationInProgress = bidderRows.some(function (bidder) {
      return operationIsActive({ status: bidder.evaluation_status });
    });
    var vendorSelect = byId("manual-review-vendor");
    var selectedEvaluationVendor = vendorSelect && vendorSelect.value
      ? vendorSelect.value : manualReviewVendor();
    var selectedEvaluationInProgress = bidderRows.some(function (bidder) {
      return bidder.vendor_id === selectedEvaluationVendor &&
        operationIsActive({ status: bidder.evaluation_status });
    });
    var selectedEvaluationComplete = bidderRows.some(function (bidder) {
      return bidder.vendor_id === selectedEvaluationVendor &&
        operationIsReady({ status: bidder.evaluation_status });
    });
    var selectedBidderState = bidderRows.find(function (bidder) {
      return bidder.vendor_id === selectedEvaluationVendor;
    });
    var selectedStatus = selectedBidderState ? operationStatus({ status: selectedBidderState.evaluation_status }) : "not_started";
    var runningBidderNames = bidderRows.filter(function (bidder) {
      return operationIsActive({ status: bidder.evaluation_status });
    }).map(function (bidder) {
      return bidder.bidder_name || bidder.vendor_id;
    });
    var retryingBidderNames = bidderRows.filter(function (bidder) {
      return operationStatus({ status: bidder.evaluation_status }) === "retrying";
    }).map(function (bidder) {
      return bidder.bidder_name || bidder.vendor_id;
    });
    byId("run-intake-btn").disabled = !frozen || !ctx.state || !ctx.state.vendors.length ||
      pendingClassification || (manualReviewOnly() && !pendingManualIntake);
    byId("run-intake-btn").textContent = manualReviewOnly() ? "Organize bids for review" :
      pendingClassification ? "Approve all classifications first" : "Prepare bidder facts";
    byId("run-evaluate-btn").disabled = selectedEvaluationInProgress || manualReviewOnly() ||
      (!evaluationComplete && idx < ctx.STAGES.indexOf("bids_intaken"));
    byId("run-evaluate-btn").hidden = manualReviewOnly() || classificationReviewActive ||
      Boolean(ctx.state && ctx.state.vendors.some(bidderDocumentsPrepared) &&
        idx < ctx.STAGES.indexOf("bids_intaken"));
    var approvedBidderCount = ctx.state && Array.isArray(ctx.state.bidders)
      ? ctx.state.bidders.filter(function (bidder) { return bidder.classification_status === "approved"; }).length
      : 0;
    byId("run-evaluate-all-btn").hidden = byId("run-evaluate-btn").hidden || approvedBidderCount < 2;
    byId("run-evaluate-all-btn").disabled = byId("run-evaluate-btn").disabled;
    byId("run-evaluate-btn").textContent = selectedEvaluationInProgress
      ? "Evaluation running…"
      : selectedEvaluationComplete || selectedStatus === "partial"
      ? "Re-run evaluation"
      : "Run evaluation";
    byId("run-evaluate-all-btn").textContent = bidderRows.some(function (bidder) {
      return bidder.classification_status === "approved" &&
        (operationIsReady({ status: bidder.evaluation_status }) ||
          operationStatus({ status: bidder.evaluation_status }) === "partial");
    })
      ? "Re-run all evaluations"
      : "Run evaluation for all approved bidders";
    setStatusText("evaluation-status", "");
    bidderRows.forEach(function (bidder) {
      var classificationJob = bidder.classification_operation || {};
      var classificationStatus = operationStatus(classificationJob);
      var name = bidder.bidder_name || bidder.vendor_id;
      if (["queued", "running", "retrying", "failed"].indexOf(classificationStatus) !== -1) {
        showOperationActivity("evaluation-status", "classification:" + bidder.vendor_id, {
          title: "Classifying " + name,
          payload: classificationJob,
          next: classificationStatus === "failed"
            ? "Review the error, then re-analyze this bidder."
            : "You can review another bidder while this finishes. The page updates automatically.",
        });
      }
      var evaluationStatus = operationStatus({
        status: bidder.evaluation_status,
        error: bidder.evaluation_error,
        error_code: bidder.evaluation_error_code,
      });
      if (["queued", "running", "retrying", "failed"].indexOf(evaluationStatus) !== -1) {
        showOperationActivity("evaluation-status", "evaluation:" + bidder.vendor_id, {
          title: "Evaluating " + name,
          payload: {
            status: bidder.evaluation_status,
            error: bidder.evaluation_error,
            error_code: bidder.evaluation_error_code,
          },
          next: evaluationStatus === "failed"
            ? "Review the error, then run this bidder again. Other results were preserved."
            : "You can review another bidder while this finishes. Results update automatically.",
        });
      }
    });
    if (!Object.keys(ctx.operationActivities["evaluation-status"] || {}).length) {
      setStatusText("evaluation-status", selectedStatus === "failed"
        ? operationErrorText(selectedBidderState, "Evaluation failed for the selected bidder.")
        : retryingBidderNames.length
        ? "Retrying evaluation for " + retryingBidderNames.join(", ") + ". Other bidders remain available."
        : evaluationComplete
        ? "Evaluation complete. Open Results, or re-run evaluation for this bidder."
        : evaluationInProgress
        ? "Evaluation is running for " + runningBidderNames.join(", ") +
          ". Other bidders and completed results remain available."
        : selectedStatus === "partial"
        ? "Evaluation completed with review items for the selected bidder."
        : selectedStatus === "not_started"
        ? "Evaluation has not started for the selected bidder yet."
        : "");
    }
    if (evaluationInProgress && !ctx.evaluationPollTimer) {
      ctx.evaluationPollTimer = window.setTimeout(function () {
        ctx.evaluationPollTimer = null;
        refreshState().catch(updateStepper);
      }, 3000);
    } else if (!evaluationInProgress && ctx.evaluationPollTimer) {
      window.clearTimeout(ctx.evaluationPollTimer);
      ctx.evaluationPollTimer = null;
    }
    byId("step-6-action-title").textContent = evaluationComplete
      ? "Approved bidder documents"
      : classificationReviewActive || manualReviewOnly() ? "Review bidder documents" : "Evaluate";
    byId("evaluate-help").textContent = evaluationComplete
      ? "Evaluation is complete. Open Results for the current scoring, or re-run evaluation for one bidder or all approved bidders."
      : classificationReviewActive || manualReviewOnly()
      ? "Confirm what each submitted file contains. Split combined PDFs into page ranges and correct a document category when needed."
      : "Evaluate each bidder against the approved tender requirements using verified document evidence.";
    byId("evaluate-lock").hidden = classificationReviewActive ||
      (manualReviewOnly() && ctx.state.vendors.some(bidderIntakeComplete)) ||
      idx >= ctx.STAGES.indexOf("bids_intaken");
    byId("recommendations").hidden = manualReviewOnly();
    byId("manual-review-workspace").hidden = !(
      ctx.state && ctx.state.vendors.some(bidderDocumentsPrepared)
    );
    byId("back-to-bidders").setAttribute("data-href", pageUrl("bids"));
    byId("return-to-bidder-uploads").href = pageUrl("bids");
    updateClassificationContinue();
    byId("bid-intake-help").textContent = manualReviewOnly()
      ? "Automated qualification checks were skipped. Add one ZIP per bidder to organize and inspect the submissions for manual review; no automated pass/fail result will be produced."
      : "Add one bidder at a time. Enter the bidder name and upload one ZIP containing that bidder's complete submission. Price material is quarantined automatically before technical extraction.";
    byId("review-log-panel").classList.toggle("locked", !ctx.projectId);
    byId("limitation-banner").hidden = !(
      ctx.state && ctx.state.artifacts.indexOf("contract_limitations") !== -1
    );
    var complianceBanner = byId("compliance-gate-banner");
    if (complianceBlocksContract()) {
      var complianceSnapshot = complianceInfo();
      complianceBanner.textContent = complianceSnapshot.status !== "completed"
        ? "Tender compliance: the policy audit has not completed — run it in the Compliance step before working on criteria."
        : "Tender compliance: " + (complianceSnapshot.open || 0) +
          " open finding(s) — resolve them in the Compliance step before working on criteria.";
      complianceBanner.hidden = false;
    } else {
      complianceBanner.textContent = "";
      complianceBanner.hidden = true;
    }
    var openChecklistBtn = byId("open-checklist-btn");
    if (openChecklistBtn) {
      openChecklistBtn.textContent = (complianceInfo() && complianceInfo().enabled)
        ? "Proceed to compliance audit"
        : "Review evaluation checklist";
    }
    var backToLedgerBtn = byId("back-to-ledger-btn");
    if (backToLedgerBtn) {
      backToLedgerBtn.textContent = (complianceInfo() && complianceInfo().enabled)
        ? "Back to compliance"
        : "Back to tender review";
    }
    renderBidderRoster();
  }

  function refreshState() {
    if (!ctx.projectId) return Promise.resolve();
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId)).then(
      function (res) {
        if (res.ok) {
          ctx.state = res.data;
          updateStepper();
        }
      }
    );
  }


  Object.assign(ctx, {
    stageIndex: stageIndex,
    complianceInfo: complianceInfo,
    complianceDone: complianceDone,
    complianceBlocksContract: complianceBlocksContract,
    manualReviewOnly: manualReviewOnly,
    bidderIntakeComplete: bidderIntakeComplete,
    bidderRecord: bidderRecord,
    bidderClassificationApproved: bidderClassificationApproved,
    bidderDocumentsPrepared: bidderDocumentsPrepared,
    pendingBidderClassifications: pendingBidderClassifications,
    updateClassificationContinue: updateClassificationContinue,
    bidderMetadata: bidderMetadata,
    saveBidderMetadata: saveBidderMetadata,
    bidderSlug: bidderSlug,
    routeProject: routeProject,
    pageUrl: pageUrl,
    recordStepView: recordStepView,
    loadActivePage: loadActivePage,
    navigateWorkflow: navigateWorkflow,
    applyPageMode: applyPageMode,
    recommendedPage: recommendedPage,
    pill: pill,
    updateStepper: updateStepper,
    refreshState: refreshState,
  });
}
