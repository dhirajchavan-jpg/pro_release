"use strict";
/* bidder intake. */

export function installBidIntake(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var bidderClassificationApproved = function () { return ctx.bidderClassificationApproved.apply(ctx, arguments); };
  var bidderDocumentsPrepared = function () { return ctx.bidderDocumentsPrepared.apply(ctx, arguments); };
  var bidderIntakeComplete = function () { return ctx.bidderIntakeComplete.apply(ctx, arguments); };
  var bidderMetadata = function () { return ctx.bidderMetadata.apply(ctx, arguments); };
  var bidderRecord = function () { return ctx.bidderRecord.apply(ctx, arguments); };
  var bidderSlug = function () { return ctx.bidderSlug.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var refreshState = function () { return ctx.refreshState.apply(ctx, arguments); };
  var rememberOperation = function () { return ctx.rememberOperation.apply(ctx, arguments); };
  var renderReportSummary = function () { return ctx.renderReportSummary.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var saveBidderMetadata = function () { return ctx.saveBidderMetadata.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };
  var updateClassificationContinue = function () { return ctx.updateClassificationContinue.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  var waitForWorkbenchJob = function () { return ctx.waitForWorkbenchJob.apply(ctx, arguments); };
  // ---------------------------------------------------------------- Step 5

  function addBid() {
    if (!ctx.projectId) return;
    var displayName = byId("bidder-name").value.trim();
    var file = byId("bid-file").files[0];
    if (!displayName) return showMessage("Enter the bidder name.", "error");
    if (!file) return showMessage("Choose one PDF or ZIP file.", "error");
    var suffix = file.name.toLowerCase().slice(file.name.lastIndexOf("."));
    if (suffix !== ".pdf" && suffix !== ".zip") {
      return showMessage("Upload one PDF or ZIP containing this bidder's complete submission.", "error");
    }
    var label = bidderSlug(displayName);
    if (ctx.state && ctx.state.vendors.indexOf(label) !== -1) {
      return showMessage("This bidder has already been added.", "error");
    }
    var form = new FormData();
    form.append("vendor_label", displayName);
    form.append("received_at", nowIso());
    form.append("file", file);
    showMessage("Uploading bid for " + label + "…");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids", {
      method: "POST",
      body: form,
    }).then(function (res) {
      if (!res.ok) return showMessage(apiError(res.data, "Could not add the bid."), "error");
      saveBidderMetadata(res.data.vendor_id || label, displayName, file.name);
      ctx.state = res.data.state;
      byId("bidder-name").value = "";
      byId("bid-file").value = "";
      updateStepper();
      showMessage("Bidder added: " + displayName + ".", "ok");
    });
  }

  function deleteBid(vendor) {
    var record = bidderRecord(vendor);
    var name = record.bidder_name || vendor;
    if (!window.confirm("Delete " + name + " and its uploaded bidder package? This cannot be undone.")) return;
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" + encodeURIComponent(vendor), {
      method: "DELETE",
    }).then(function (res) {
      if (!res.ok) return showMessage(apiError(res.data, "Could not delete the bidder."), "error");
      var metadata = bidderMetadata();
      delete metadata[vendor];
      try {
        localStorage.setItem("workbench.bidders." + ctx.projectId, JSON.stringify(metadata));
      } catch (_error) {
        // Browser storage is optional.
      }
      ctx.state = res.data.state;
      updateStepper();
      showMessage("Bidder deleted: " + name + ".", "ok");
    });
  }

  function replaceBid(vendor) {
    var record = bidderRecord(vendor);
    var name = record.bidder_name || vendor;
    var input = el("input", { type: "file", accept: ".pdf,.zip" });
    input.addEventListener("change", function () {
      var file = input.files[0];
      if (!file) return;
      var reason = window.prompt("Reason for replacing " + name + "'s bidder package:", "Corrected bidder submission");
      if (!reason || !reason.trim()) return showMessage("A replacement reason is required.", "error");
      var form = new FormData();
      form.append("file", file);
      form.append("reason", reason.trim());
      showMessage("Replacing bidder package for " + name + "…");
      api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/bids/" +
        encodeURIComponent(vendor) + "/replace", { method: "POST", body: form }).then(function (res) {
        if (!res.ok) return showMessage(apiError(res.data, "Could not replace the bidder package."), "error");
        saveBidderMetadata(vendor, name, file.name);
        ctx.state = res.data.state;
        updateStepper();
        showMessage("Bidder package replaced. Process the corrected documents before evaluation.", "ok");
      });
    });
    input.click();
  }

  function renderBidderRoster() {
    var list = byId("vendor-list");
    list.textContent = "";
    if (!ctx.state) return;
    var metadata = bidderMetadata();
    if (!ctx.state.vendors.length) {
      list.appendChild(el("div", { className: "vendor-empty", text: "No bidders added yet." }));
    }
    ctx.state.vendors.forEach(function (vendor) {
      var meta = metadata[vendor] || {};
      var row = el("div", { className: "vendor-row" });
      var identity = el("div");
      identity.appendChild(el("strong", { text: meta.displayName || vendor }));
      identity.appendChild(el("small", { text: "Submission ID: " + vendor }));
      var packageCell = el("div");
      packageCell.appendChild(el("strong", { text: meta.fileName || "Package uploaded" }));
      packageCell.appendChild(el("small", {
        text: meta.packageBytes
          ? (meta.packageBytes / 1048576).toFixed(1) + " MB · one isolated bidder submission"
          : "One isolated bidder submission",
      }));
      var status = bidderClassificationApproved(vendor)
        ? "Classification approved"
        : bidderDocumentsPrepared(vendor)
          ? "Classification needs approval"
          : bidderIntakeComplete(vendor)
            ? "Intake complete"
            : "Ready to prepare documents";
      row.appendChild(identity);
      row.appendChild(packageCell);
      row.appendChild(pill(status, bidderClassificationApproved(vendor) ? "complete" : "review_note"));
      if (bidderDocumentsPrepared(vendor)) {
        row.appendChild(el("a", {
          className: "vendor-action",
          href: pageUrl("evaluate") + "?vendor=" + encodeURIComponent(vendor),
          text: bidderClassificationApproved(vendor) ? "View classification" : "Review classification",
        }));
      }
      var replace = el("button", {
        className: "secondary",
        type: "button",
        text: "Replace package",
        title: "Upload a corrected PDF or ZIP and retain the revision history.",
      });
      replace.addEventListener("click", function () { replaceBid(vendor); });
      row.appendChild(replace);
      var remove = el("button", {
        className: "secondary vendor-delete",
        type: "button",
        text: "Delete bidder",
      });
      remove.disabled = bidderClassificationApproved(vendor) || Boolean(bidderRecord(vendor).evaluation_id);
      remove.title = remove.disabled
        ? "Approved or evaluated bidders cannot be deleted."
        : "Delete this bidder and its uploaded package.";
      remove.addEventListener("click", function () { deleteBid(vendor); });
      row.appendChild(remove);
      list.appendChild(row);
    });
    var selector = byId("report-vendors");
    var initialized = selector.getAttribute("data-initialized") === "true";
    var selected = new Set(Array.from(selector.querySelectorAll("input:checked")).map(function (input) { return input.value; }));
    selector.textContent = "";
    ctx.state.vendors.forEach(function (vendor) {
      var checkbox = el("input", { type: "checkbox", value: vendor });
      checkbox.checked = !initialized || selected.has(vendor);
      checkbox.addEventListener("change", renderReportSummary);
      selector.appendChild(el("label", { className: "report-vendor-option" }, [
        checkbox,
        el("span", { text: (metadata[vendor] || {}).displayName || vendor }),
      ]));
    });
    selector.setAttribute("data-initialized", "true");
  }

  function runIntake() {
    if (!ctx.projectId || !requireReviewer()) return;
    var targetVendor = ctx.state.vendors.find(function (vendor) {
      return !bidderDocumentsPrepared(vendor);
    }) || ctx.state.vendors[0];
    showOperationActivity("intake-status", "intake", {
      title: "Preparing bidder documents",
      status: "queued",
      detail: "Starting document preparation…",
      next: "You can continue reviewing this tender. Classification will appear when ready.",
    });
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/intake", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewed_by: reviewer(), reviewed_at: nowIso() }),
    }).then(function (res) {
      if (!res.ok) {
        showOperationActivity("intake-status", "intake", {
          title: "Document preparation could not start",
          status: "failed",
          detail: apiError(res.data, "Intake failed."),
          next: "Review the error, then process the documents again.",
        });
        updateClassificationContinue();
        return showMessage(apiError(res.data, "Intake failed."), "error");
      }
      if (!res.data.job_id) return showMessage("Bidder intake did not return a job.", "error");
      rememberOperation("intake", res.data.job_id, "project");
      showOperationActivity("intake-status", "intake", {
        title: "Preparing bidder documents",
        status: "queued",
        detail: "The job has been accepted and is waiting to start…",
        next: "You can continue reviewing this tender. Classification will appear when ready.",
      });
      return waitForWorkbenchJob(res.data.job_id, {
        kind: "intake",
        scope: "project",
        statusElement: byId("intake-status"),
        onUpdate: function (payload, status) {
          showOperationActivity("intake-status", "intake", {
            title: "Preparing bidder documents",
            payload: payload,
            status: status === "status_unavailable" || status === "stalled" ? "running" : "",
            detail: status === "status_unavailable"
              ? "Status is temporarily unavailable; document preparation may still be running."
              : status === "stalled"
              ? "This is taking longer than usual, but document preparation is still running."
              : "",
            next: "You can continue reviewing this tender. Classification will appear when ready.",
          });
        },
      }).then(function () {
        return refreshState();
      }).then(function () {
        updateStepper();
        setStatusText("intake-status", "Documents prepared. Review and approve the classification before evaluation.");
        showMessage("Bidder documents are ready for classification review.", "ok");
        window.location.href = pageUrl("evaluate") + "?vendor=" + encodeURIComponent(targetVendor);
      });
    }).catch(function (errorValue) {
      showOperationActivity("intake-status", "intake", {
        title: "Bidder document preparation failed",
        status: "failed",
        detail: errorValue.message || "Intake failed.",
        next: "Review the error, then process the documents again.",
      });
      updateClassificationContinue();
      showMessage(errorValue.message || "Intake failed.", "error");
    });
  }

  function continueToEvaluation() {
    byId("continue-to-evaluation-btn").disabled = true;
    byId("classification-continue-status").textContent = "Preparing bidder facts for classification review…";
    runIntake();
  }


  Object.assign(ctx, {
    addBid: addBid,
    deleteBid: deleteBid,
    replaceBid: replaceBid,
    renderBidderRoster: renderBidderRoster,
    runIntake: runIntake,
    continueToEvaluation: continueToEvaluation,
  });
}
