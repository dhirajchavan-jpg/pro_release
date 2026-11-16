"use strict";
/* Evaluation result loading and reviewer actions. */

import { reviewPanelId } from "./results-model.js";

export function installResultsActions(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var renderResults = function () { return ctx.renderResults.apply(ctx, arguments); };
  var renderReviewLog = function () { return ctx.renderReviewLog.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };

  function loadResults() {
    var force = arguments[0] === true;
    if (!ctx.projectId || (!force && ctx.resultsLoadedProject === ctx.projectId)) return Promise.resolve();
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/results").then(
      function (res) {
        if (!res.ok) {
          showMessage(apiError(res.data, "Results could not be loaded."), "error");
          return;
        }
        renderResults(
          res.data.vendors || {},
          res.data.unevaluated_requirements || [],
          res.data.comparison || {},
          Boolean(res.data.complete)
        );
        ctx.resultsLoadedProject = ctx.projectId;
      }
    );
  }

  function renderHumanReview(vendor, criterion) {
    var existing = criterion.human_review;
    var panel = el("div", { className: "human-review-panel", id: reviewPanelId(vendor, criterion.criterion_id, criterion.checkpoint_id) });
    panel.hidden = true;
    panel.appendChild(el("h4", { text: "Does the bidder meet this requirement?" }));
    panel.appendChild(el("p", { text: "Check the cited document, then answer Yes, No, or Cannot confirm. Your answer is saved to the audit trail." }));
    var decision = el("select", { "aria-label": "Human decision" });
    [
      ["", "Select an answer"],
      ["meets_requirement", "Yes — meets the requirement"],
      ["does_not_meet", "No — does not meet the requirement"],
      ["clarification_required", "Cannot confirm from the evidence"],
    ].forEach(function (item) {
      var option = el("option", { value: item[0], text: item[1] });
      if (existing && existing.decision === item[0]) option.setAttribute("selected", "selected");
      decision.appendChild(option);
    });
    var reason = el("textarea", {
      rows: "4",
      "aria-label": "Human review reason",
      placeholder: "Optional for Yes. For No or Cannot confirm, explain what is wrong or missing.",
    });
    reason.value = existing ? existing.reason : "";
    var correctedValue = el("textarea", {
      rows: "3",
      "aria-label": "Corrected bidder value",
      placeholder: "Optional: correct the AI-extracted bidder value.",
    });
    correctedValue.value = existing ? (existing.corrected_value || "") : "";
    var grid = el("div", { className: "human-review-grid" }, [
      el("div", { className: "field" }, [el("label", { text: "Answer" }), decision]),
      el("div", { className: "field" }, [el("label", { text: "Review note (optional for Yes)" }), reason]),
    ]);
    if ((criterion.review_codes || []).indexOf("competing_facts") !== -1 || (existing && existing.corrected_value)) {
      grid.insertBefore(
        el("div", { className: "field" }, [el("label", { text: "Correct value" }), correctedValue]),
        grid.lastChild
      );
    }
    panel.appendChild(grid);
    if (existing) panel.appendChild(el("p", {
      className: "human-review-saved",
      text: "Last reviewed by " + existing.reviewed_by + " on " + new Date(existing.reviewed_at).toLocaleString(),
    }));
    var save = el("button", { type: "button", text: existing ? "Update human decision" : "Save human decision" });
    var close = el("button", { className: "secondary", type: "button", text: "Close" });
    var status = el("span", { className: "status-line" });
    close.addEventListener("click", function () {
      panel.hidden = true;
      var row = panel.closest("tr");
      if (row) row.hidden = true;
    });
    save.addEventListener("click", function () {
      if (!requireReviewer()) return;
      var reviewReason = reason.value.trim();
      if (!decision.value || (decision.value !== "meets_requirement" && reviewReason.length < 10)) {
        status.textContent = "Choose an answer. Explain the reason when the answer is No or Cannot confirm.";
        return;
      }
      if (!reviewReason) reviewReason = "Confirmed from the cited bidder evidence.";
      save.disabled = true;
      status.textContent = "Saving reviewed decision…";
      api(
        "/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/vendors/" + encodeURIComponent(vendor) +
        "/criteria/" + encodeURIComponent(criterion.criterion_id) + "/human-review",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision: decision.value,
            checkpoint_id: criterion.checkpoint_id || "",
            corrected_value: correctedValue.value.trim(),
            reason: reviewReason,
            reviewed_by: reviewer(),
            reviewed_at: nowIso(),
          }),
        }
      ).then(function (res) {
        if (!res.ok) {
          save.disabled = false;
          status.textContent = apiError(res.data, "The human decision could not be saved.");
          return;
        }
        showMessage("Human decision saved to the audit trail.", "ok");
        ctx.reportLoadedProject = null;
        loadResults(true);
      });
    });
    panel.appendChild(el("div", { className: "human-review-actions" }, [save, close, status]));
    return panel;
  }

  function loadReviewLog() {
    var wrap = byId("review-log-wrap");
    if (!ctx.projectId) {
      wrap.textContent = "";
      byId("review-log-preview").textContent = "";
      byId("review-log-details").hidden = true;
      return;
    }
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/review-log").then(
      function (res) {
        if (!res.ok) return;
        renderReviewLog(res.data);
      }
    );
  }

  return {
    loadResults: loadResults,
    renderHumanReview: renderHumanReview,
    loadReviewLog: loadReviewLog,
  };
}
