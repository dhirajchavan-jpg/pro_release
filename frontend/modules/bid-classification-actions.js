"use strict";
/* Bidder classification loading, corrections, approval, and evaluation actions. */

import {
  classificationChanged,
  classificationRow,
  comparableClassification,
  comparablePhysicalRoutes,
  manualDocumentLabel,
  validateClassification,
} from "./bid-classification-model.js";

export function evaluationFailureIsCurrent(vendorId, bidders, isReady) {
  var bidder = (bidders || []).find(function (item) { return item.vendor_id === vendorId; });
  return !bidder || !isReady({ status: bidder.evaluation_status });
}

export function installBidClassificationActions(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var bidderDocumentsPrepared = function () { return ctx.bidderDocumentsPrepared.apply(ctx, arguments); };
  var bidderMetadata = function () { return ctx.bidderMetadata.apply(ctx, arguments); };
  var bidderRecord = function () { return ctx.bidderRecord.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var navigateWorkflow = function () { return ctx.navigateWorkflow.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var operationIsReady = function () { return ctx.operationIsReady.apply(ctx, arguments); };
  var operationStatus = function () { return ctx.operationStatus.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pendingBidderClassifications = function () { return ctx.pendingBidderClassifications.apply(ctx, arguments); };
  var refreshState = function () { return ctx.refreshState.apply(ctx, arguments); };
  var rememberOperation = function () { return ctx.rememberOperation.apply(ctx, arguments); };
  var renderClassificationWorkspace = function () { return ctx.renderClassificationWorkspace.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  var waitForOperation = function () { return ctx.waitForOperation.apply(ctx, arguments); };
  var waitForWorkbenchJob = function () { return ctx.waitForWorkbenchJob.apply(ctx, arguments); };

  function manualReviewVendor() {
    return new URLSearchParams(window.location.search).get("vendor") || "";
  }

  function prepareManualReview() {
    var select = byId("manual-review-vendor");
    var metadata = bidderMetadata();
    var vendors = ctx.state.vendors.filter(bidderDocumentsPrepared);
    var requested = manualReviewVendor();
    select.textContent = "";
    vendors.forEach(function (vendor) {
      select.appendChild(el("option", {
        value: vendor,
        text: (metadata[vendor] || {}).displayName || vendor,
      }));
    });
    if (!vendors.length) {
      byId("manual-review-status").textContent = "Run bidder intake before reviewing documents.";
      return Promise.resolve();
    }
    select.value = vendors.indexOf(requested) !== -1 ? requested : vendors[0];
    if (select.value !== requested) {
      window.history.replaceState({}, "", pageUrl("evaluate") + "?vendor=" + encodeURIComponent(select.value));
    }
    return loadManualBidReview(select.value);
  }

  function approveClassification() {
    if (!ctx.projectId || !ctx.activeClassification || !requireReviewer()) return;
    var error = validateClassification(ctx, byId);
    if (error) return showMessage(error, "error");
    if (classificationChanged(ctx)) return showMessage("Save your changes before approval.", "error");
    var vendor = ctx.activeClassification.vendor;
    var base = "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" +
      encodeURIComponent(vendor) + "/classification";
    byId("approve-classification-btn").disabled = true;
    byId("manual-review-status").textContent = "Approving classification…";
    api(base + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reviewed_by: reviewer(),
        reviewed_at: nowIso(),
        reason: "Confirmed the saved document classification against the submitted PDFs.",
      }),
    }).then(function (approved) {
      if (!approved.ok) throw new Error(apiError(approved.data, "The classification could not be approved."));
      return api("/workbench/projects/" + encodeURIComponent(ctx.projectId)).then(function (project) {
        if (!project.ok) throw new Error(apiError(project.data, "The project state could not be refreshed."));
        ctx.state = project.data;
        updateStepper();
        var pending = pendingBidderClassifications();
        if (pending.length) {
          var nextVendor = pending[0];
          byId("manual-review-vendor").value = nextVendor;
          window.history.replaceState({}, "", pageUrl("evaluate") + "?vendor=" + encodeURIComponent(nextVendor));
          showMessage("Classification approved. " + pending.length + " bidder" +
            (pending.length === 1 ? " remains" : "s remain") + " to review.", "ok");
          loadManualBidReview(nextVendor);
          return;
        }
        showMessage("All bidder classifications are approved. Run evaluation when ready.", "ok");
        loadManualBidReview(vendor);
        window.setTimeout(function () { byId("continue-to-evaluation-btn").focus(); }, 0);
      });
    }).catch(function (errorValue) {
      byId("approve-classification-btn").disabled = false;
      showMessage(errorValue.message || "The classification could not be approved.", "error");
    });
  }

  function saveClassification() {
    if (!ctx.projectId || !ctx.activeClassification || !requireReviewer()) return;
    var error = validateClassification(ctx, byId);
    if (error) {
      showMessage(error, "error");
      if (classificationChanged(ctx) && !byId("classification-reason").value.trim()) {
        byId("classification-reason").focus();
      }
      return;
    }
    if (!classificationChanged(ctx)) return showMessage("There are no unsaved changes.", "ok");
    var vendor = ctx.activeClassification.vendor;
    var path = "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" +
      encodeURIComponent(vendor) + "/classification/correct";
    byId("save-classification-btn").disabled = true;
    byId("manual-review-status").textContent = "Saving changes…";
    var savePayload = {
      reviewed_by: reviewer(),
      reviewed_at: nowIso(),
      reason: byId("classification-reason").value.trim(),
    };
    if (JSON.stringify(comparableClassification(ctx.activeClassification.rows)) !==
        JSON.stringify(comparableClassification(ctx.activeClassification.savedRows))) {
      savePayload.logical_documents = ctx.activeClassification.rows.map(classificationRow);
    }
    var changedRoutes = Object.keys(ctx.activeClassification.physicalRoutes).filter(function (documentId) {
      var current = ctx.activeClassification.physicalRoutes[documentId] || {};
      var saved = ctx.activeClassification.savedPhysicalRoutes[documentId] || {};
      return JSON.stringify(comparablePhysicalRoutes({ current: current })) !==
        JSON.stringify(comparablePhysicalRoutes({ current: saved }));
    }).map(function (documentId) {
      var route = ctx.activeClassification.physicalRoutes[documentId];
      return {
        document_id: documentId,
        broad_route: route.broad_route,
        secondary_routes: route.secondary_routes || [],
        evidence_purposes: route.evidence_purposes || [],
        reason: route.reason || "",
      };
    });
    if (changedRoutes.length) savePayload.physical_routes = changedRoutes;
    api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(savePayload),
    }).then(function (saved) {
      if (!saved.ok) throw new Error(apiError(saved.data, "The changes could not be saved."));
      showMessage("Classification changes saved.", "ok");
      loadManualBidReview(vendor);
    }).catch(function (errorValue) {
      byId("save-classification-btn").disabled = false;
      showMessage(errorValue.message || "The changes could not be saved.", "error");
    });
  }

  function improveClassification() {
    if (!ctx.projectId || !ctx.activeClassification) return;
    if (classificationChanged(ctx) && !window.confirm("Discard the unsaved classification changes and re-analyze these documents?")) return;
    var vendor = ctx.activeClassification.vendor;
    var button = byId("propose-classification-btn");
    button.disabled = true;
    button.textContent = "Re-analyzing\u2026";
    button.setAttribute("aria-busy", "true");
    button.classList.add("button-loading");
    showMessage("Re-analyzing bidder documents. Keep this page open while processing completes.", "ok");
    showOperationActivity("manual-review-status", "classification:" + vendor, {
      title: "Classifying " + (bidderRecord(vendor).bidder_name || vendor),
      status: "queued",
      detail: "Reading the bidder package and rebuilding document sections…",
      next: "You can review another bidder while this finishes. Progress is saved automatically.",
    });
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" +
      encodeURIComponent(vendor) + "/classification/propose", { method: "POST" }).then(function (res) {
      if (!res.ok) throw new Error(apiError(res.data, "The document classification could not be refreshed."));
      return waitForWorkbenchJob(res.data.job_id, {
        kind: "classification",
        scope: vendor,
        onUpdate: function (payload, status) {
          showOperationActivity("manual-review-status", "classification:" + vendor, {
            title: "Classifying " + (bidderRecord(vendor).bidder_name || vendor),
            payload: payload,
            status: status === "status_unavailable" || status === "stalled" ? "running" : "",
            detail: status === "status_unavailable"
              ? "Status is temporarily unavailable; classification may still be running."
              : status === "stalled"
              ? "This is taking longer than usual, but classification is still running."
              : "",
            next: "You can review another bidder while this finishes. Progress is saved automatically.",
          });
        },
      });
    }).then(function () {
      showMessage("The bidder package was reclassified. Review the proposed sections before approval.", "ok");
      loadManualBidReview(vendor);
    }).catch(function (errorValue) {
      showOperationActivity("manual-review-status", "classification:" + vendor, {
        title: "Classification failed for " + (bidderRecord(vendor).bidder_name || vendor),
        status: "failed",
        detail: errorValue.message || "The document classification could not be refreshed.",
        next: "Review the error, then re-analyze this bidder.",
      });
      showMessage(errorValue.message || "The document classification could not be refreshed.", "error");
    }).finally(function () {
      button.disabled = false;
      button.textContent = "Re-analyze documents";
      button.removeAttribute("aria-busy");
      button.classList.remove("button-loading");
    });
  }

  function loadManualBidReview(vendor) {
    if (!ctx.projectId || !vendor) return Promise.resolve();
    var requestNumber = ++ctx.manualReviewRequest;
    var frame = byId("manual-pdf-frame");
    var previous = frame.getAttribute("data-object-url");
    if (previous) URL.revokeObjectURL(previous);
    frame.removeAttribute("src");
    frame.removeAttribute("data-object-url");
    ctx.activeDocumentFrame = null;
    byId("manual-pdf-status").textContent = "Choose a submitted file to inspect its source.";
    byId("manual-review-status").textContent = "Loading bidder documents…";
    var base = "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" + encodeURIComponent(vendor);
    return Promise.all([api(base + "/classification"), api(base + "/review")]).then(function (responses) {
      if (requestNumber !== ctx.manualReviewRequest) return;
      var classification = responses[0];
      var review = responses[1];
      if (!review.ok) throw new Error(apiError(review.data, "The bidder documents could not be loaded."));
      showMessage("", "");
      if (classification.ok) return renderClassificationWorkspace(classification.data, review.data);
      var fallbackRows = (review.data.documents || []).map(function (document) {
        return {
          logical_document_id: document.document_id + "-section-1",
          title: manualDocumentLabel(document, []),
          document_type: document.document_type || "other",
          source_document_id: document.document_id,
          start_page: 1,
          end_page: Number(document.page_count || 1),
        };
      });
      renderClassificationWorkspace({
        vendor_id: vendor,
        status: "draft",
        document_types: fallbackRows.map(function (row) { return row.document_type; }).concat(["other"]),
        classification: { ai_proposal: fallbackRows, logical_documents: fallbackRows },
      }, review.data);
      byId("save-classification-btn").disabled = true;
      byId("approve-classification-btn").disabled = true;
      byId("manual-review-status").textContent = "Classification is not available for this intake yet.";
    }).catch(function (errorValue) {
      byId("manual-review-status").textContent = "The bidder documents could not be loaded.";
      showMessage(errorValue.message || "The bidder documents could not be loaded.", "error");
    });
  }

  function runEvaluate(allBidders) {
    if (!ctx.projectId || !requireReviewer()) return;
    var vendorSelect = byId("manual-review-vendor");
    var selectedVendor = allBidders
      ? ""
      : vendorSelect && !vendorSelect.disabled && vendorSelect.value
      ? vendorSelect.value
      : new URLSearchParams(window.location.search).get("vendor");
    var evaluateButton = allBidders && byId("run-evaluate-all-btn") ? byId("run-evaluate-all-btn") : byId("run-evaluate-btn");
    var evaluateLabel = evaluateButton.textContent || (allBidders ? "Run evaluation for all approved bidders" : "Run evaluation");
    var forceRerun = allBidders
      ? (ctx.state && ctx.state.bidders || []).some(function (bidder) {
        return bidder.classification_status === "approved" &&
          (operationIsReady({ status: bidder.evaluation_status }) ||
            operationStatus({ status: bidder.evaluation_status }) === "partial");
      })
      : Boolean(selectedVendor) && (ctx.state && ctx.state.bidders || []).some(function (bidder) {
        return bidder.vendor_id === selectedVendor &&
          (operationIsReady({ status: bidder.evaluation_status }) ||
            operationStatus({ status: bidder.evaluation_status }) === "partial");
      });
    if (forceRerun && !window.confirm(
      allBidders
        ? "This will replace existing bidder results and may incur additional AI cost. Run all approved bidders again?"
        : "This will replace the existing bidder result and may incur additional AI cost. Run this bidder again?"
    )) return;
    evaluateButton.disabled = true;
    ctx.resultsLoadedProject = null;
    ctx.reportLoadedProject = null;
    evaluateButton.textContent = "Evaluating…";
    setStatusText("evaluation-status", "");
    (ctx.state && ctx.state.bidders || []).filter(function (bidder) {
      return bidder.classification_status === "approved" && (!selectedVendor || bidder.vendor_id === selectedVendor);
    }).forEach(function (bidder) {
      showOperationActivity("evaluation-status", "evaluation:" + bidder.vendor_id, {
        title: "Evaluating " + (bidder.bidder_name || bidder.vendor_id),
        status: "queued",
        detail: "Starting bidder evaluation…",
        next: "You can review another bidder while this finishes. Results update automatically.",
      });
    });
    showMessage(allBidders ? "Preparing evaluation for all approved bidders…" : "Preparing bidder evaluation…");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/evaluate" + (forceRerun ? "?force=true" : ""), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectedVendor ? { vendor_id: selectedVendor } : {}),
    }).then(function (res) {
      if (!res.ok) {
        evaluateButton.disabled = false;
        evaluateButton.textContent = evaluateLabel;
        showOperationActivity("evaluation-status", "evaluation:start", {
          title: "Evaluation could not start",
          status: "failed",
          detail: apiError(res.data, "Evaluation failed."),
          next: "Review the error, then run the evaluation again.",
        });
        return showMessage(apiError(res.data, "Evaluation failed."), "error");
      }
      ctx.state = res.data.state;
      updateStepper();
      var operations = res.data.operations || [];
      var skipped = res.data.skipped_vendors || [];
      showMessage(
        operations.length
          ? "Evaluation started for " + operations.length + " bidder(s)…"
          : "The selected bidder already has a current result.",
        "ok"
      );
      if (skipped.length) {
        showMessage(
          "Evaluation started for approved bidders. Document review is still pending for: " +
            skipped.join(", ") + ".",
          "ok"
        );
      }
      var failedVendors = [];
      var settleOutcomes = [];
      return Promise.allSettled(operations.map(function (item) {
        rememberOperation("evaluation", item.operation_id, item.vendor_id || "project");
        return waitForOperation(item.operation_id, {
          vendorId: item.vendor_id,
          vendorName: item.vendor_name || item.vendor_id,
        });
      })).then(function (outcomes) {
        settleOutcomes = outcomes;
        failedVendors = outcomes.reduce(function (items, outcome, index) {
          if (outcome.status === "rejected") items.push(operations[index].vendor_id || "Unknown bidder");
          return items;
        }, []);
        return refreshState();
      }).then(function () {
        failedVendors = failedVendors.filter(function (vendorId) {
          return evaluationFailureIsCurrent(vendorId, ctx.state && ctx.state.bidders, operationIsReady);
        });
        if (failedVendors.length) {
          evaluateButton.disabled = false;
          evaluateButton.textContent = evaluateLabel;
          var failureDetails = settleOutcomes.reduce(function (items, outcome, index) {
            var vendorId = operations[index].vendor_id || "Unknown bidder";
            if (outcome.status === "rejected" && failedVendors.indexOf(vendorId) !== -1) {
              items.push(vendorId + ": " +
                (outcome.reason && outcome.reason.message || "evaluation failed"));
            }
            return items;
          }, []);
          showMessage("Evaluation failed for: " + failureDetails.join("; ") + ". Successful bidder results were preserved.", "error");
          return;
        }
        navigateWorkflow("results", pageUrl("results"), true);
      }).catch(function (errorValue) {
        evaluateButton.disabled = false;
        evaluateButton.textContent = evaluateLabel;
        showMessage(errorValue.message || "Evaluation failed.", "error");
      });
    });
  }

  function handleEvaluationAction() {
    runEvaluate();
  }

  return {
    manualReviewVendor: manualReviewVendor,
    prepareManualReview: prepareManualReview,
    approveClassification: approveClassification,
    saveClassification: saveClassification,
    improveClassification: improveClassification,
    loadManualBidReview: loadManualBidReview,
    runEvaluate: runEvaluate,
    handleEvaluationAction: handleEvaluationAction,
  };
}
