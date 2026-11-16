"use strict";
/* Checklist loading, editing, saving, and approval actions. */

import {
  criterionIncluded,
  normalizeDraft,
} from "./checklist-model.js";

export function installChecklistActions(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiDetails = function () { return ctx.apiDetails.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var applyPageMode = function () { return ctx.applyPageMode.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var blockingQuestions = function () { return ctx.blockingQuestions.apply(ctx, arguments); };
  var complianceBlocksContract = function () { return ctx.complianceBlocksContract.apply(ctx, arguments); };
  var complianceInfo = function () { return ctx.complianceInfo.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var evidenceDetails = function () { return ctx.evidenceDetails.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var refreshState = function () { return ctx.refreshState.apply(ctx, arguments); };
  var renderBlockingQuestions = function () { return ctx.renderBlockingQuestions.apply(ctx, arguments); };
  var renderDraft = function () { return ctx.renderDraft.apply(ctx, arguments); };
  var renderMessage = function () { return ctx.renderMessage.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var routeProject = function () { return ctx.routeProject.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var stageIndex = function () { return ctx.stageIndex.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };

  function loadDraft() {
    if (!ctx.projectId) return Promise.resolve(false);
    if (ctx.draftDirty && !window.confirm("Discard the unsaved checklist changes and reload the saved version?")) {
      return Promise.resolve(false);
    }
    var createDraft = ctx.state && ctx.state.stage === "rfp_understood";
    var reloadButton = byId("load-draft-btn");
    reloadButton.disabled = true;
    reloadButton.setAttribute("aria-busy", "true");
    byId("contract-criteria").setAttribute("aria-busy", "true");
    byId("contract-criteria").textContent = "Loading evaluation checklist…";
    renderMessage(byId("freeze-errors"), "");
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/contract/draft", {
      method: createDraft ? "POST" : "GET",
    }).then(function (res) {
      reloadButton.disabled = false;
      reloadButton.removeAttribute("aria-busy");
      byId("contract-criteria").removeAttribute("aria-busy");
      if (!res.ok) {
        byId("contract-criteria").textContent = "";
        renderMessage(byId("freeze-errors"), apiError(res.data, "The evaluation checklist could not be loaded."), "error");
        return false;
      }
      ctx.draft = createDraft ? res.data.draft : (res.data.draft || res.data);
      if (res.data.state) ctx.state = res.data.state;
      normalizeDraft(ctx);
      ctx.draftDirty = false;
      ctx.changedCriterionIds = Object.create(null);
      ctx.addedCriterionIds = Object.create(null);
      ctx.sourceReviewDirty = false;
      ctx.selectedCriterionId = null;
      ctx.checklistEditMode = false;
      ctx.blockersRevealed = false;
      byId("checklist-change-reason").value = "";
      updateStepper();
      renderDraft();
      return true;
    });
  }

  function markCriterionChanged(criterion, preserveConfirmation) {
    criterion.review = criterion.review || {};
    if (!preserveConfirmation) {
      criterion.review.confirmed = false;
      criterion.review.confirmed_by = "";
      criterion.review.confirmed_at = "";
    }
    ctx.changedCriterionIds[criterion.criterion_id] = true;
    ctx.draftDirty = true;
    ctx.blockersRevealed = false;
    updateChecklistSaveState();
    updateFreezeButton();
  }

  function updateChecklistSaveState() {
    var saveButton = byId("save-contract-draft-btn");
    var saveBar = byId("checklist-save-bar");
    saveButton.disabled = !ctx.draftDirty || ctx.draftSaveInFlight;
    saveButton.setAttribute("aria-busy", ctx.draftSaveInFlight ? "true" : "false");
    saveBar.classList.toggle("has-changes", ctx.draftDirty || ctx.draftSaveInFlight);
    byId("checklist-save-state").textContent = ctx.draftSaveInFlight
      ? "Saving checklist…"
      : ctx.draftDirty ? "Unsaved changes" : "No unsaved changes";
  }

  function openChecklist() {
    if (!ctx.projectId) return;
    var currentRoute = routeProject();
    if (currentRoute && currentRoute.page === "ledger") {
      if (complianceInfo() && complianceInfo().enabled) {
        window.location.href = pageUrl("compliance");
      } else {
        window.location.href = pageUrl("contract");
      }
      return;
    }
    if (complianceBlocksContract()) {
      var openCount = (complianceInfo() && complianceInfo().open) || 0;
      showMessage(
        openCount > 0
          ? "Resolve or authorize all " + openCount + " compliance finding(s) before proceeding to the evaluation checklist."
          : "Run the tender compliance audit before proceeding to the evaluation checklist.",
        "error"
      );
      return;
    }
    var button = byId("open-checklist-btn");
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    var proceedBtn = byId("proceed-to-contract-btn");
    if (proceedBtn) {
      proceedBtn.disabled = true;
      proceedBtn.setAttribute("aria-busy", "true");
    }
    loadDraft().then(function (loaded) {
      if (button) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
      if (proceedBtn) {
        proceedBtn.disabled = false;
        proceedBtn.removeAttribute("aria-busy");
      }
      if (!loaded) return;
      window.location.href = pageUrl("contract");
    });
  }

  function ensureTenderDocuments() {
    if (ctx.tenderDocuments) return Promise.resolve(ctx.tenderDocuments);
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/documents").then(function (res) {
      if (!res.ok) throw new Error(apiError(res.data, "Source documents are unavailable."));
      ctx.tenderDocuments = res.data.documents || [];
      return ctx.tenderDocuments;
    });
  }

  function showCriterionSource(criterion, host, button) {
    var refs = criterion.rfp_source_refs || [];
    if (!refs.length) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    host.textContent = "Loading source preview…";
    ensureTenderDocuments().then(function (documents) {
      var enriched = refs.map(function (ref) {
        var document = documents.find(function (item) { return item.document_id === ref.document_id; });
        var copy = Object.assign({}, ref);
        if (document && document.page_url_template && ref.page) {
          copy.page_image_url = document.page_url_template.replace("{page}", String(ref.page));
        }
        return copy;
      });
      host.textContent = "";
      var preview = evidenceDetails(enriched);
      preview.open = true;
      host.appendChild(preview);
    }).catch(function (errorValue) {
      host.textContent = errorValue.message || "Source preview could not be loaded.";
    }).then(function () {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    });
  }

  function updateFreezeButton() {
    var frozen = stageIndex() >= ctx.STAGES.indexOf("contract_frozen");
    var hasIncluded = ctx.draft && ctx.draft.criteria.some(criterionIncluded);
    var hasBlockers = ctx.draft && blockingQuestions().length > 0;
    byId("freeze-contract-btn").disabled = (!ctx.draft && !frozen) || ctx.freezeInFlight || ctx.draftSaveInFlight || ctx.draftDirty || !hasIncluded || hasBlockers;
    byId("freeze-contract-btn").textContent = frozen ? "Continue to bids" : "Approve checklist and continue";
  }

  function attemptContinue() {
    var route = routeProject();
    if (!route || route.page !== "contract") {
      window.location.href = pageUrl("contract");
      return;
    }
    if (stageIndex() >= ctx.STAGES.indexOf("contract_frozen")) {
      window.location.href = pageUrl("bids");
      return;
    }
    if (!ctx.draft) return;
    if (ctx.draftDirty) {
      byId("checklist-change-reason").focus();
      return showMessage("Save the checklist changes before approval.", "error");
    }
    if (blockingQuestions().length) {
      ctx.blockersRevealed = true;
      renderBlockingQuestions();
      byId("contract-questions").scrollIntoView({ behavior: "smooth", block: "start" });
      showMessage("Resolve or explicitly exclude every included check that needs attention.", "error");
      return;
    }
    freezeContract(false, false);
  }

  function addChecklistCriterion() {
    if (!ctx.draft) return;
    var id = "NEW-" + (window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : Date.now().toString(36));
    var criterion = {
      criterion_id: id,
      title: "New evaluation check",
      family: "other",
      applicability: "applicable",
      evaluation_scope: "technical_qualification",
      materiality: "mandatory_blocking",
      rule: {
        rule_family: "other",
        requirement: "",
        mandatory: true,
        failure_effect: "manual_review",
        pass_rule: "all_required_evidence_must_pass",
        required_documents: [],
      },
      rfp_source_refs: [],
      missing_fields: [],
      needs_human_input: false,
      depends_on: [],
      notes: "Human-added check. Add the requirement and expected proof before saving.",
      review: {
        included: true,
        origin: "human_added",
        exclusion_reason: "",
        change_reason: "",
      },
    };
    ctx.draft.criteria.push(criterion);
    ctx.addedCriterionIds[id] = true;
    markCriterionChanged(criterion);
    ctx.selectedCriterionId = id;
    ctx.checklistEditMode = true;
    byId("checklist-search").value = "";
    byId("checklist-family-filter").value = "all";
    byId("checklist-state-filter").value = "all";
    byId("checklist-origin-filter").value = "all";
    renderDraft();
    var card = byId("criterion-" + id);
    if (card) {
      card.open = true;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      var title = card.querySelector("input");
      if (title) {
        title.select();
        title.focus();
      }
    }
  }

  function saveContractDraft() {
    if (!ctx.projectId || !ctx.draft || !ctx.draftDirty || ctx.draftSaveInFlight) return;
    if (!requireReviewer()) return;
    var reason = byId("checklist-change-reason").value.trim();
    if (reason.length < 10) {
      showMessage("Enter a short reason (at least 10 characters) for the checklist changes before saving.", "error");
      byId("checklist-change-reason").focus();
      return;
    }
    var invalid = ctx.draft.criteria.find(function (criterion) {
      return criterionIncluded(criterion) && (
        !String(criterion.title || "").trim() ||
        (criterion.review.origin === "human_added" && !String(criterion.rule.requirement || "").trim())
      );
    });
    if (invalid) {
      var missingTitle = !String(invalid.title || "").trim();
      byId("checklist-search").value = "";
      byId("checklist-family-filter").value = "all";
      byId("checklist-state-filter").value = "all";
      byId("checklist-origin-filter").value = "all";
      ctx.selectedCriterionId = invalid.criterion_id;
      ctx.checklistEditMode = true;
      renderDraft();
      byId("checklist-detail-panel").scrollIntoView({ behavior: "smooth", block: "center" });
      showMessage(
        missingTitle
          ? "Complete the Check title for this new check, or exclude it before saving."
          : "Complete the Requirement field for ‘" + invalid.title + "’, or exclude the check before saving.",
        "error"
      );
      return;
    }
    ctx.draft.criteria.forEach(function (criterion) {
      if (!ctx.changedCriterionIds[criterion.criterion_id]) return;
      criterion.review.change_reason = reason;
      if (!criterionIncluded(criterion)) criterion.review.exclusion_reason = reason;
    });
    var unexplainedExclusion = ctx.draft.criteria.find(function (criterion) {
      return !criterionIncluded(criterion) && !String(criterion.review.exclusion_reason || "").trim();
    });
    if (unexplainedExclusion) {
      showMessage("Every excluded check needs an exclusion reason.", "error");
      return;
    }
    var payload = {
      draft: ctx.draft,
      revision: ctx.draft.revision,
      reason: reason,
      reviewed_by: reviewer(),
      reviewed_at: nowIso(),
    };
    ctx.draftSaveInFlight = true;
    updateChecklistSaveState();
    updateFreezeButton();
    renderMessage(byId("freeze-errors"), "Saving checklist changes…");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/contract/draft", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      ctx.draftSaveInFlight = false;
      if (!res.ok) {
        updateChecklistSaveState();
        updateFreezeButton();
        if (res.status === 409) {
          renderMessage(byId("freeze-errors"), "This checklist changed on the server. Your edits are still visible; reload the checklist before trying to save again.", "error");
          return;
        }
        renderMessage(byId("freeze-errors"), apiError(res.data, "The checklist changes could not be saved."), "error");
        return;
      }
      ctx.draft = res.data.draft || res.data;
      if (res.data.state) ctx.state = res.data.state;
      normalizeDraft(ctx);
      ctx.draftDirty = false;
      ctx.changedCriterionIds = Object.create(null);
      ctx.addedCriterionIds = Object.create(null);
      ctx.sourceReviewDirty = false;
      byId("checklist-change-reason").value = "";
      updateStepper();
      renderDraft();
      renderMessage(byId("freeze-errors"), "Checklist changes saved.", "ok");
    });
  }

  function freezeContract(continueWithLimitations, askConfirmation) {
    if (!ctx.projectId || !ctx.draft || ctx.freezeInFlight) return;
    var route = routeProject();
    if (!route || route.page !== "contract") {
      window.location.href = pageUrl("contract");
      return;
    }
    if (stageIndex() >= ctx.STAGES.indexOf("contract_frozen")) {
      window.location.href = pageUrl("bids");
      return;
    }
    if (!requireReviewer()) return;
    continueWithLimitations = continueWithLimitations === true;
    if (continueWithLimitations && askConfirmation) {
      var sourceGapCount = (ctx.draft.unautomated_requirements || []).length;
      var limitationMessage = sourceGapCount
        ? sourceGapCount + " tender source passage" + (sourceGapCount === 1 ? " could" : "s could") + " not be classified reliably. Continue with these passages recorded as limitations? The final result will remain incomplete until they are reviewed."
        : "Continue with a limited evaluation? Unresolved checks and checks that depend on them will be excluded. Results will be incomplete and require human review.";
      if (!window.confirm(limitationMessage)) return;
    }
    var payload = {
      draft: ctx.draft,
      reviewed_by: reviewer(),
      reviewed_at: nowIso(),
      continue_with_limitations: continueWithLimitations,
    };
    ctx.freezeInFlight = true;
    updateFreezeButton();
    showMessage("Approving the bidder checks…");
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/contract/freeze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) {
        var details = apiDetails(res.data);
        if (res.status === 409 && details.missing_fields) {
          var box = byId("freeze-errors");
          box.textContent = "";
          box.className = "message error";
          box.appendChild(
            el("strong", { text: "These bidder checks still need human input:" })
          );
          Object.keys(details.missing_fields).forEach(function (criterionId) {
            box.appendChild(
              el("p", {
                text: criterionId + ": " + details.missing_fields[criterionId].join(", "),
              })
            );
            var card = byId("criterion-" + criterionId);
            if (card) card.classList.add("needs-input");
          });
          ctx.freezeInFlight = false;
          updateFreezeButton();
          return;
        }
        var sourceCoverageBlocked = details.code === "CHECKLIST_SOURCE_COVERAGE_INCOMPLETE"
          || Array.isArray(details.requirements)
          || /source requirements remain unresolved/i.test(apiError(res.data, ""));
        if (res.status === 409 && sourceCoverageBlocked && !continueWithLimitations) {
          ctx.freezeInFlight = false;
          updateFreezeButton();
          freezeContract(true, true);
          return;
        }
        if (res.status === 409) {
          return refreshState().then(function () {
            if (stageIndex() >= ctx.STAGES.indexOf("contract_frozen")) {
              window.location.href = pageUrl("bids");
              return;
            }
            ctx.freezeInFlight = false;
            updateFreezeButton();
            showMessage(apiError(res.data, "The bidder checks could not be approved."), "error");
          });
        }
        ctx.freezeInFlight = false;
        updateFreezeButton();
        return showMessage(apiError(res.data, "The bidder checks could not be approved."), "error");
      }
      ctx.state = res.data.state;
      updateStepper();
      window.history.replaceState({}, "", pageUrl("bids"));
      applyPageMode("bids");
      ctx.freezeInFlight = false;
      var skipped = Object.keys(res.data.skipped_criteria || {});
      showMessage(skipped.length
        ? "Limited evaluation started. " + skipped.length + " unresolved check" + (skipped.length === 1 ? " was" : "s were") + " excluded."
        : "Bidder checks approved (" + res.data.frozen_criteria.length + "). Add vendor bids next.", "ok");
    });
  }

  return {
    loadDraft: loadDraft,
    markCriterionChanged: markCriterionChanged,
    updateChecklistSaveState: updateChecklistSaveState,
    openChecklist: openChecklist,
    ensureTenderDocuments: ensureTenderDocuments,
    showCriterionSource: showCriterionSource,
    updateFreezeButton: updateFreezeButton,
    attemptContinue: attemptContinue,
    addChecklistCriterion: addChecklistCriterion,
    saveContractDraft: saveContractDraft,
    freezeContract: freezeContract,
  };
}
