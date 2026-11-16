"use strict";
/* Checklist rendering and interaction views. */

import {
  CHECKLIST_GROUPS,
  CHECKLIST_GROUP_ORDER,
  MATERIALITY_LABELS,
  ORIGIN_LABELS,
  criterionConfirmed,
  criterionDisplayParts,
  criterionGroup,
  criterionImpact,
  criterionIncluded,
  criterionNeedsAttention,
  criterionTerms,
  criterionTitle as findCriterionTitle,
  evaluationCheckGroups,
  friendlyKey,
  friendlyMissing,
  friendlyStatus,
  missingParts,
  normalizeDraft,
  ruleValueToText,
  coerceRuleValue,
} from "./checklist-model.js";

export function installChecklistView(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var applyPageMode = function () { return ctx.applyPageMode.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var markCriterionChanged = function () { return ctx.markCriterionChanged.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var showCriterionSource = function () { return ctx.showCriterionSource.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var updateChecklistSaveState = function () { return ctx.updateChecklistSaveState.apply(ctx, arguments); };
  var updateFreezeButton = function () { return ctx.updateFreezeButton.apply(ctx, arguments); };

  function criterionTitle(criterionId) {
    return findCriterionTitle(ctx, criterionId);
  }

  function resolveSourceGap(item, addCheck) {
    if (!addCheck && !window.confirm("Mark this passage as not requiring a bidder-evaluation check? This decision will be recorded when you save the checklist.")) return;
    ctx.draft.unautomated_requirements = ctx.draft.unautomated_requirements.filter(function (candidate) { return candidate !== item; });
    if (addCheck) {
      var id = "NEW-" + (window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : Date.now().toString(36));
      var quote = String(item.source_quote || item.requirement || "Tender requirement").trim();
      var criterion = {
        criterion_id: id,
        source_gap_id: item.source_id || item.criterion_id || "",
        title: quote.length > 180 ? quote.slice(0, 177) + "…" : quote,
        family: "other",
        applicability: "unresolved",
        evaluation_scope: "technical_qualification",
        materiality: "advisory",
        rule: {
          rule_family: "other",
          requirement: quote,
          mandatory: false,
          failure_effect: "manual_review",
          pass_rule: "Reviewer confirms that the bidder satisfies this tender requirement.",
          required_documents: [],
        },
        rfp_source_refs: [],
        missing_fields: [],
        needs_human_input: true,
        depends_on: [],
        notes: "Created from a tender source passage during checklist review.",
        review: { included: true, origin: "human_added", exclusion_reason: "", change_reason: "" },
      };
      Object.defineProperty(criterion, "_sourceGap", { value: item });
      ctx.draft.criteria.push(criterion);
      ctx.addedCriterionIds[id] = true;
      markCriterionChanged(criterion);
      ctx.selectedCriterionId = id;
      byId("checklist-family-filter").value = "all";
      byId("checklist-state-filter").value = "unresolved";
      if (!byId("checklist-change-reason").value.trim()) {
        byId("checklist-change-reason").value = "Converted an unclassified tender passage into a bidder-evaluation check.";
      }
    } else {
      ctx.draftDirty = true;
      ctx.sourceReviewDirty = true;
      ctx.blockersRevealed = false;
      if (!byId("checklist-change-reason").value.trim()) {
        byId("checklist-change-reason").value = "Confirmed that the reviewed tender passage is not a bidder-evaluation requirement.";
      }
    }
    renderDraft();
  }

  function renderDraft() {
    var host = byId("contract-criteria");
    host.textContent = "";
    normalizeDraft(ctx);
    renderChecklistSummary();
    renderChecklistFamilyFilter();
    renderChecklistOriginFilter();
    renderChecklistCategoryNav();
    var selectedFamily = byId("checklist-family-filter").value || "all";
    var selectedState = byId("checklist-state-filter").value || "all";
    var sourceGaps = Array.isArray(ctx.draft.unautomated_requirements)
      ? ctx.draft.unautomated_requirements
      : [];
    var showSourceGaps = sourceGaps.length && selectedFamily === "all" && (selectedState === "all" || selectedState === "unresolved");
    if (showSourceGaps) {
      var notice = el("details", { className: "criterion-card needs-input" });
      notice.open = selectedState === "unresolved";
      notice.appendChild(el("summary", {
        className: "criterion-head",
        text: sourceGaps.length + " tender source passage" + (sourceGaps.length === 1 ? " needs" : "s need") + " review",
      }));
      var body = el("div", { className: "provenance" });
      body.appendChild(el("p", { text: "The system could not reliably decide whether these passages create bidder-evaluation checks. They are not assigned to the selected checklist category." }));
      sourceGaps.forEach(function (item) {
        var documentLabel = item.document_id
          ? friendlyKey(String(item.document_id).replace(/^tender[-_]?/i, "Tender document "))
          : "Tender source";
        var location = documentLabel + (item.page ? " · page " + item.page : "");
        var row = el("div", { className: "checklist-source-gap" });
        row.appendChild(el("strong", { text: location }));
        row.appendChild(el("blockquote", {
          text: item.source_quote || "No readable source text was recovered for this passage.",
        }));
        if (item.requirement && !String(item.requirement).startsWith("This source unit could not be validated")) {
          row.appendChild(el("small", { text: "Review reason: " + item.requirement }));
        }
        var actions = el("div", { className: "question-actions" });
        var addCheck = el("button", { type: "button", text: "Add as evaluation check" });
        addCheck.addEventListener("click", function () { resolveSourceGap(item, true); });
        actions.appendChild(addCheck);
        var dismiss = el("button", { className: "secondary", type: "button", text: "Not a bidder check" });
        dismiss.addEventListener("click", function () { resolveSourceGap(item, false); });
        actions.appendChild(dismiss);
        row.appendChild(actions);
        body.appendChild(row);
      });
      notice.appendChild(body);
      host.appendChild(notice);
    }
    var visibleCriteria = ctx.draft.criteria.filter(criterionMatchesFilters);
    var visibleIds = Object.create(null);
    visibleCriteria.forEach(function (criterion) { visibleIds[criterion.criterion_id] = true; });
    var visibleGroups = evaluationCheckGroups(ctx.draft).map(function (group) {
      return {
        group: group,
        criteria: group.criterion_ids.map(function (criterionId) {
          return ctx.draft.criteria.find(function (criterion) { return criterion.criterion_id === criterionId; });
        }).filter(function (criterion) { return criterion && visibleIds[criterion.criterion_id]; }),
      };
    }).filter(function (entry) { return entry.criteria.length; });
    var selectedCriterion = visibleCriteria.find(function (criterion) { return criterion.criterion_id === ctx.selectedCriterionId; });
    if (!selectedCriterion) {
      selectedCriterion = visibleCriteria.find(criterionNeedsAttention) || visibleCriteria[0] || null;
      ctx.selectedCriterionId = selectedCriterion ? selectedCriterion.criterion_id : null;
      ctx.checklistEditMode = false;
    }
    visibleGroups.forEach(function (entry) {
      host.appendChild(renderEvaluationGroupRow(entry.group, entry.criteria));
    });
    if (!visibleCriteria.length && !showSourceGaps) {
      host.appendChild(el("div", { className: "checklist-empty" }, [
        el("strong", { text: "No checks match these filters." }),
        el("p", { text: "Clear the search or select a different category or status." }),
      ]));
    }
    selectedFamily = byId("checklist-family-filter").value;
    byId("checklist-list-title").textContent = selectedFamily === "all"
      ? "All evaluation checks"
      : (CHECKLIST_GROUPS[selectedFamily] || CHECKLIST_GROUPS.other).label;
    byId("checklist-filter-status").textContent = visibleGroups.length + " grouped checks · " + visibleCriteria.length + " requirements" +
      (showSourceGaps ? " · " + sourceGaps.length + " source passages to review" : "");
    var selectedGroup = visibleGroups.find(function (entry) {
      return selectedCriterion && entry.criteria.some(function (criterion) { return criterion.criterion_id === selectedCriterion.criterion_id; });
    });
    renderCriterionDetail(selectedCriterion, selectedGroup && selectedGroup.group, selectedGroup && selectedGroup.criteria);
    renderBlockingQuestions();
    updateChecklistSaveState();
    updateFreezeButton();
  }

  function renderChecklistCategoryNav() {
    var host = byId("checklist-category-nav");
    var selected = byId("checklist-family-filter").value || "all";
    host.textContent = "";
    var choices = [{ key: "all", label: "All checks", description: "Complete evaluation basis" }];
    CHECKLIST_GROUP_ORDER.forEach(function (family) {
      if (!ctx.draft.criteria.some(function (criterion) { return criterionGroup(criterion) === family; })) return;
      choices.push({ key: family, label: CHECKLIST_GROUPS[family].label, description: CHECKLIST_GROUPS[family].description });
    });
    choices.forEach(function (choice) {
      var criteria = choice.key === "all" ? ctx.draft.criteria : ctx.draft.criteria.filter(function (criterion) { return criterionGroup(criterion) === choice.key; });
      var criterionIds = Object.create(null);
      criteria.forEach(function (criterion) { criterionIds[criterion.criterion_id] = true; });
      var groups = evaluationCheckGroups(ctx.draft).filter(function (group) {
        return group.criterion_ids.some(function (criterionId) { return criterionIds[criterionId]; });
      });
      var attention = groups.filter(function (group) {
        return group.criterion_ids.some(function (criterionId) {
          var criterion = criteria.find(function (item) { return item.criterion_id === criterionId; });
          return criterion && criterionNeedsAttention(criterion);
        });
      }).length + (choice.key === "all" ? (ctx.draft.unautomated_requirements || []).length : 0);
      var includedCount = groups.filter(function (group) {
        return group.criterion_ids.some(function (criterionId) {
          var criterion = criteria.find(function (item) { return item.criterion_id === criterionId; });
          return criterion && criterionIncluded(criterion);
        });
      }).length;
      var button = el("button", {
        className: "checklist-category-button" + (selected === choice.key ? " active" : ""),
        type: "button",
        title: choice.description,
        "aria-label": choice.label + ", " + groups.length + " grouped checks covering " + criteria.length + " requirements" + (attention ? ", " + attention + " need attention" : ""),
        "aria-pressed": selected === choice.key ? "true" : "false",
      });
      var label = el("span");
      label.appendChild(el("strong", { text: choice.label }));
      label.appendChild(el("small", {
        text: includedCount + " groups · " + criteria.length + " requirements" + (attention ? " · " + attention + " to review" : ""),
      }));
      button.appendChild(label);
      button.appendChild(el("b", { className: attention ? "attention" : "", text: String(groups.length) }));
      button.addEventListener("click", function () {
        byId("checklist-family-filter").value = choice.key;
        ctx.selectedCriterionId = null;
        ctx.checklistEditMode = false;
        renderDraft();
      });
      host.appendChild(button);
    });
  }

  function renderEvaluationGroupRow(group, criteria) {
    var includedCriteria = criteria.filter(criterionIncluded);
    var included = includedCriteria.length > 0;
    var attention = criteria.some(criterionNeedsAttention);
    var confirmed = included && includedCriteria.every(criterionConfirmed);
    var selected = criteria.some(function (criterion) { return criterion.criterion_id === ctx.selectedCriterionId; });
    var proofs = [];
    criteria.forEach(function (criterion) {
      (criterion.rule.required_documents || []).forEach(function (documentName) {
        if (proofs.indexOf(documentName) === -1) proofs.push(documentName);
      });
    });
    var materialities = criteria.map(function (criterion) { return criterion.materiality; }).filter(function (value, index, values) {
      return value && values.indexOf(value) === index;
    });
    var row = el("button", {
      className: "checklist-row" + (selected ? " selected" : "") + (attention ? " needs-attention" : "") + (!included ? " excluded" : ""),
      type: "button",
      id: "criterion-group-row-" + group.group_id,
      "aria-pressed": selected ? "true" : "false",
    });
    var title = el("span", { className: "checklist-row-title" });
    title.appendChild(el("strong", { text: group.title }));
    title.appendChild(el("small", {
      text: criteria.length + " requirement" + (criteria.length === 1 ? "" : "s") + (group.source_page ? " · page " + group.source_page : ""),
    }));
    row.appendChild(title);
    row.appendChild(el("span", { className: "checklist-row-type", text: materialities.length === 1 ? (MATERIALITY_LABELS[materialities[0]] || friendlyKey(materialities[0])) : "Mixed treatment" }));
    row.appendChild(el("span", { className: "checklist-row-proof", text: proofs.length ? proofs.length + " proof item" + (proofs.length === 1 ? "" : "s") : "No specific proof" }));
    row.appendChild(el("span", {
      className: "checklist-row-status " + (!included ? "excluded" : confirmed ? "confirmed" : attention ? "attention" : "ready"),
      text: !included ? "Excluded" : confirmed ? "Confirmed" : attention ? "Needs attention" : "Ready",
    }));
    row.appendChild(el("span", { className: "checklist-row-arrow", text: "›", "aria-hidden": "true" }));
    row.addEventListener("click", function () {
      var next = criteria.find(criterionNeedsAttention) || includedCriteria[0] || criteria[0];
      ctx.selectedCriterionId = next.criterion_id;
      ctx.checklistEditMode = false;
      renderDraft();
    });
    return row;
  }

  function toggleCriterionIncluded(criterion) {
    criterion.review.included = !criterionIncluded(criterion);
    if (criterion.review.included) criterion.review.exclusion_reason = "";
    markCriterionChanged(criterion);
    renderDraft();
  }

  function setCriterionConfirmation(criterion, confirmed) {
    if (!requireReviewer()) return;
    criterion.review.confirmed = confirmed;
    criterion.review.confirmed_by = confirmed ? reviewer() : "";
    criterion.review.confirmed_at = confirmed ? nowIso() : "";
    if (!byId("checklist-change-reason").value.trim()) {
      byId("checklist-change-reason").value = confirmed
        ? "Reviewed and confirmed the checklist check as correct."
        : "Removed the earlier checklist confirmation for further review.";
    }
    markCriterionChanged(criterion, true);
    renderDraft();
    showMessage(
      confirmed
        ? "Check confirmed. Save the checklist update to record this review."
        : "Confirmation removed. Save the checklist update to record the change.",
      "ok"
    );
  }

  function discardAddedCriterion(criterion) {
    if (criterion._sourceGap) ctx.draft.unautomated_requirements.push(criterion._sourceGap);
    ctx.draft.criteria = ctx.draft.criteria.filter(function (candidate) {
      return candidate.criterion_id !== criterion.criterion_id;
    });
    delete ctx.changedCriterionIds[criterion.criterion_id];
    delete ctx.addedCriterionIds[criterion.criterion_id];
    ctx.draftDirty = ctx.sourceReviewDirty || Object.keys(ctx.changedCriterionIds).length > 0;
    ctx.selectedCriterionId = null;
    ctx.checklistEditMode = false;
    if (!ctx.draftDirty) byId("checklist-change-reason").value = "";
    renderDraft();
    showMessage("Added check discarded.", "ok");
  }

  function renderCriterionDetail(criterion, group, groupCriteria) {
    var panel = byId("checklist-detail-panel");
    panel.textContent = "";
    panel.className = "checklist-detail-pane" + (ctx.checklistEditMode ? " editing" : "");
    if (!criterion) {
      panel.appendChild(el("div", { className: "checklist-detail-empty" }, [
        el("strong", { text: "Select an evaluation check" }),
        el("p", { text: "Its requirement, bidder proof, tender evidence, and edit controls will appear here." }),
      ]));
      return;
    }
    var display = criterionDisplayParts(criterion);
    var header = el("div", { className: "checklist-detail-head" });
    var copy = el("div");
    copy.appendChild(el("span", { className: "eyebrow", text: (CHECKLIST_GROUPS[criterionGroup(criterion)] || CHECKLIST_GROUPS.other).label }));
    copy.appendChild(el("h3", { text: group && group.title ? group.title : display.requirement }));
    header.appendChild(copy);
    var actions = el("div", { className: "checklist-detail-actions" });
    if (criterionNeedsAttention(criterion) || criterionConfirmed(criterion)) {
      var confirm = el("button", {
        type: "button",
        text: criterionConfirmed(criterion) ? "Undo confirmation" : "Confirm as correct",
        className: criterionConfirmed(criterion) ? "secondary" : "",
      });
      confirm.addEventListener("click", function () {
        setCriterionConfirmation(criterion, !criterionConfirmed(criterion));
      });
      actions.appendChild(confirm);
    }
    var edit = el("button", { className: "secondary", type: "button", text: ctx.checklistEditMode ? "Done editing" : "Edit check" });
    edit.addEventListener("click", function () {
      ctx.checklistEditMode = !ctx.checklistEditMode;
      renderCriterionDetail(criterion, group, groupCriteria);
    });
    actions.appendChild(edit);
    if (ctx.addedCriterionIds[criterion.criterion_id]) {
      var discard = el("button", { className: "secondary danger-action", type: "button", text: "Discard check" });
      discard.addEventListener("click", function () { discardAddedCriterion(criterion); });
      actions.appendChild(discard);
    } else {
      var includeToggle = el("button", {
        className: criterionIncluded(criterion) ? "secondary danger-action" : "secondary",
        type: "button",
        text: criterionIncluded(criterion) ? "Exclude check" : "Restore check",
      });
      includeToggle.addEventListener("click", function () { toggleCriterionIncluded(criterion); });
      actions.appendChild(includeToggle);
    }
    header.appendChild(actions);
    panel.appendChild(header);
    groupCriteria = groupCriteria || [criterion];
    if (groupCriteria.length > 1) {
      var subchecks = el("div", { className: "checklist-subcheck-nav" });
      groupCriteria.forEach(function (item, index) {
        var itemDisplay = criterionDisplayParts(item);
        var button = el("button", {
          type: "button",
          className: item.criterion_id === criterion.criterion_id ? "active" : "",
          text: (index + 1) + ". " + itemDisplay.requirement,
        });
        button.addEventListener("click", function () {
          ctx.selectedCriterionId = item.criterion_id;
          ctx.checklistEditMode = false;
          renderDraft();
        });
        subchecks.appendChild(button);
      });
      panel.appendChild(subchecks);
    }
    var card = renderCriterion(criterion, ctx.draft.criteria.indexOf(criterion));
    card.open = true;
    card.classList.add("detail-criterion");
    panel.appendChild(card);
  }

  function renderChecklistSummary() {
    var summary = byId("checklist-summary");
    summary.textContent = "";
    var included = ctx.draft.criteria.filter(criterionIncluded);
    var groups = evaluationCheckGroups(ctx.draft);
    var stats = [
      ["Grouped checks", groups.length],
      ["Requirements", ctx.draft.criteria.length],
      ["Included", included.length],
      ["Must pass", included.filter(function (item) { return item.rule && item.rule.mandatory === true; }).length],
      ["Scored", included.filter(function (item) { return item.materiality === "scored"; }).length],
      ["Needs attention", included.filter(criterionNeedsAttention).length + (ctx.draft.unautomated_requirements || []).length],
    ];
    stats.forEach(function (item) {
      summary.appendChild(el("div", { className: "checklist-stat" }, [
        el("span", { text: item[0] }),
        el("strong", { text: String(item[1]) }),
      ]));
    });
  }

  function renderChecklistFamilyFilter() {
    var select = byId("checklist-family-filter");
    var selected = select.value || "all";
    var families = ctx.draft.criteria.map(criterionGroup).filter(function (family, index, values) {
      return values.indexOf(family) === index;
    });
    select.textContent = "";
    select.appendChild(el("option", { value: "all", text: "All categories" }));
    CHECKLIST_GROUP_ORDER.forEach(function (family) {
      if (families.indexOf(family) === -1) return;
      select.appendChild(el("option", { value: family, text: (CHECKLIST_GROUPS[family] || CHECKLIST_GROUPS.other).label }));
    });
    select.value = families.indexOf(selected) === -1 ? "all" : selected;
  }

  function renderChecklistOriginFilter() {
    var select = byId("checklist-origin-filter");
    var selected = select.value || "all";
    var origins = ctx.draft.criteria.map(function (criterion) {
      return criterion.review.origin;
    }).filter(function (origin, index, values) {
      return origin && values.indexOf(origin) === index;
    }).sort();
    select.textContent = "";
    select.appendChild(el("option", { value: "all", text: "All sources" }));
    origins.forEach(function (origin) {
      select.appendChild(el("option", { value: origin, text: ORIGIN_LABELS[origin] || friendlyKey(origin) }));
    });
    select.value = origins.indexOf(selected) === -1 ? "all" : selected;
  }

  function criterionMatchesFilters(criterion) {
    var query = byId("checklist-search").value.trim().toLowerCase();
    var stateFilter = byId("checklist-state-filter").value;
    var familyFilter = byId("checklist-family-filter").value;
    var originFilter = byId("checklist-origin-filter").value;
    var included = criterionIncluded(criterion);
    var stateMatch = stateFilter === "all" ||
      (stateFilter === "included" && included) ||
      (stateFilter === "excluded" && !included) ||
      (stateFilter === "unresolved" && criterionNeedsAttention(criterion));
    var originMatch = originFilter === "all" || criterion.review.origin === originFilter;
    var familyMatch = familyFilter === "all" || criterionGroup(criterion) === familyFilter;
    var searchable = [
      criterion.title,
      criterion.family,
      criterion.applicability,
      criterion.evaluation_scope,
      criterion.materiality,
      JSON.stringify(criterion.rule || {}),
      JSON.stringify(criterion.rfp_source_refs || []),
    ].join(" ").toLowerCase();
    return stateMatch && familyMatch && originMatch && (!query || searchable.indexOf(query) !== -1);
  }

  function criterionField(label, value, criterion, property, disabled) {
    var field = el("div", { className: "field" });
    field.appendChild(el("label", { text: label }));
    var input = value !== null && typeof value === "object" ? el("textarea") : el("input", { type: "text" });
    input.value = value == null ? "" : ruleValueToText(value);
    input.disabled = disabled;
    input.addEventListener("change", function () {
      try {
        criterion[property] = coerceRuleValue(input.value, value);
        input.removeAttribute("data-invalid");
        markCriterionChanged(criterion);
      } catch (exc) {
        input.setAttribute("data-invalid", "true");
        showMessage(label + " has an invalid value.", "error");
      }
    });
    field.appendChild(input);
    return field;
  }

  function renderCriterion(criterion, index) {
    var included = criterionIncluded(criterion);
    var display = criterionDisplayParts(criterion);
    var terms = criterionTerms(criterion);
    var requiredDocuments = Array.isArray(criterion.rule.required_documents) ? criterion.rule.required_documents : [];
    var card = el("details", {
      className: "criterion-card" + (criterionNeedsAttention(criterion) ? " needs-input" : "") + (!included ? " excluded" : ""),
      id: "criterion-" + criterion.criterion_id,
    });
    var head = el("summary", { className: "criterion-head" });
    var titleBlock = el("span", { className: "criterion-title-block" });
    titleBlock.appendChild(el("span", { className: "title", text: display.requirement, title: criterion.criterion_id }));
    titleBlock.appendChild(el("small", { className: "criterion-topic", text: display.topic }));
    head.appendChild(titleBlock);
    var headStatus = el("span", { className: "criterion-head-status" });
    if (!included) headStatus.appendChild(pill("Excluded", "fail"));
    else if (criterionConfirmed(criterion)) headStatus.appendChild(pill("Confirmed", "pass"));
    else if (criterionNeedsAttention(criterion)) headStatus.appendChild(pill("Needs attention", "review"));
    else headStatus.appendChild(pill("Ready", "pass"));
    headStatus.appendChild(pill(MATERIALITY_LABELS[criterion.materiality] || friendlyKey(criterion.materiality || "review"), criterion.materiality === "scored" ? "review_note" : "blocked"));
    if (criterion.applicability === "conditional") headStatus.appendChild(pill("Conditional", "review_note"));
    head.appendChild(headStatus);
    card.appendChild(head);

    if (criterionConfirmed(criterion)) card.appendChild(el("p", {
      className: "criterion-guidance confirmed",
      text: "Confirmed as correct by " + (criterion.review.confirmed_by || "the reviewer") + ". Save the checklist if this confirmation is still pending.",
    }));
    else if (criterion.missing_fields.length) card.appendChild(el("p", {
      className: "criterion-guidance",
      text: included
        ? "This check needs attention before approval. Confirm it as correct, edit it, or exclude it from bidder evaluation."
        : "This check is excluded from bidder evaluation and remains in the audit trail.",
    }));

    var overview = el("div", { className: "criterion-overview" });
    var requirementPanel = el("div");
    requirementPanel.appendChild(el("strong", { text: "What will be checked" }));
    requirementPanel.appendChild(el("p", { text: display.requirement }));
    overview.appendChild(requirementPanel);
    var proofPanel = el("div");
    proofPanel.appendChild(el("strong", { text: "Expected bidder proof" }));
    if (requiredDocuments.length) {
      var proofList = el("ul", { className: "criterion-proof-list" });
      requiredDocuments.forEach(function (documentName) { proofList.appendChild(el("li", { text: String(documentName) })); });
      proofPanel.appendChild(proofList);
    } else {
      proofPanel.appendChild(el("p", { text: "No specific bidder document is required by this check." }));
    }
    overview.appendChild(proofPanel);
    var decisionPanel = el("div");
    decisionPanel.appendChild(el("strong", { text: "Evaluation treatment" }));
    var decisionTags = el("div", { className: "criterion-decision-tags" });
    decisionTags.appendChild(pill(criterionImpact(criterion), included ? "review_note" : "fail"));
    decisionTags.appendChild(pill(friendlyStatus(criterion.applicability), "review_note"));
    decisionTags.appendChild(pill(ORIGIN_LABELS[criterion.review.origin] || friendlyKey(criterion.review.origin), "review_note"));
    decisionPanel.appendChild(decisionTags);
    overview.appendChild(decisionPanel);
    card.appendChild(overview);

    if (terms.conditions.length || terms.exceptions.length) {
      var termsBox = el("details", { className: "criterion-terms" });
      termsBox.appendChild(el("summary", { text: "Conditions and exceptions (" + (terms.conditions.length + terms.exceptions.length) + ")" }));
      if (terms.conditions.length) {
        termsBox.appendChild(el("strong", { text: "Conditions" }));
        var conditionList = el("ul", { className: "criterion-proof-list" });
        terms.conditions.forEach(function (item) { conditionList.appendChild(el("li", { text: item })); });
        termsBox.appendChild(conditionList);
      }
      if (terms.exceptions.length) {
        termsBox.appendChild(el("strong", { text: "Exceptions" }));
        var exceptionList = el("ul", { className: "criterion-proof-list" });
        terms.exceptions.forEach(function (item) { exceptionList.appendChild(el("li", { text: item })); });
        termsBox.appendChild(exceptionList);
      }
      card.appendChild(termsBox);
    }

    if (criterion.depends_on.length) card.appendChild(el("p", {
      className: "criterion-review-reason",
      text: "Evaluated after: " + criterion.depends_on.map(criterionTitle).join(", "),
    }));

    card.appendChild(el("h4", { className: "criterion-definition-title", text: "Edit check definition" }));
    if (!ctx.addedCriterionIds[criterion.criterion_id]) {
      var actions = el("div", { className: "criterion-actions" });
      var includeButton = el("button", {
        className: included ? "secondary danger-action" : "secondary",
        type: "button",
        text: included ? "Exclude check" : "Restore check",
      });
      includeButton.addEventListener("click", function () {
        toggleCriterionIncluded(criterion);
        var updatedCard = byId("criterion-" + criterion.criterion_id);
        if (updatedCard) {
          updatedCard.open = true;
          updatedCard.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      actions.appendChild(includeButton);
      card.appendChild(actions);
    }

    var metadata = el("div", { className: "rule-grid criterion-metadata" });
    metadata.appendChild(criterionField("Check title", criterion.title, criterion, "title", !included));
    metadata.appendChild(criterionField("Category", criterion.family, criterion, "family", !included));
    metadata.appendChild(criterionField("Applicability", criterion.applicability, criterion, "applicability", !included));
    metadata.appendChild(criterionField("Evaluation scope", criterion.evaluation_scope, criterion, "evaluation_scope", !included));
    metadata.appendChild(criterionField("Materiality", criterion.materiality, criterion, "materiality", !included));
    card.appendChild(metadata);

    var grid = el("div", { className: "rule-grid" });
    Object.keys(criterion.rule).forEach(function (key) {
      if (key === "rule_family") return;
      var value = criterion.rule[key];
      var field = el("div", { className: "field" });
      field.appendChild(el("label", { text: friendlyKey(key), title: key }));
      var input;
      if (typeof value === "boolean") {
        input = el("select");
        ["true", "false"].forEach(function (opt) {
          var option = el("option", { value: opt, text: opt });
          if (String(value) === opt) option.setAttribute("selected", "selected");
          input.appendChild(option);
        });
      } else if (value !== null && typeof value === "object") {
        input = el("textarea");
        input.value = ruleValueToText(value);
      } else {
        input = el("input", { type: typeof value === "number" ? "number" : "text" });
        input.value = value == null ? "" : String(value);
      }
      input.disabled = !included;
      input.addEventListener("change", function () {
        try {
          criterion.rule[key] = coerceRuleValue(input.value, value);
          input.removeAttribute("data-invalid");
          markCriterionChanged(criterion);
        } catch (exc) {
          input.setAttribute("data-invalid", "true");
          showMessage(friendlyKey(key) + " has an invalid value.", "error");
        }
      });
      field.appendChild(input);
      grid.appendChild(field);
    });
    card.appendChild(grid);

    if (criterion.rfp_source_refs.length) {
      var prov = el("div", { className: "provenance" });
      prov.appendChild(el("strong", { text: "Tender evidence" }));
      criterion.rfp_source_refs.forEach(function (ref) {
        var sourceLabel = (ref.document_id || "Tender document") + (ref.page ? " p." + ref.page : "");
        prov.appendChild(
          el("blockquote", {
            text: ref.quote ? "“" + ref.quote + "” — " + sourceLabel : "Verified visually on " + sourceLabel,
          })
        );
      });
      var previewHost = el("div", { className: "criterion-source-preview", "aria-live": "polite" });
      var previewButton = el("button", { className: "secondary", type: "button", text: "Open tender evidence" });
      previewButton.addEventListener("click", function () {
        showCriterionSource(criterion, previewHost, previewButton);
      });
      prov.appendChild(previewButton);
      prov.appendChild(previewHost);
      card.appendChild(prov);
    }

    if (!included && criterion.review.exclusion_reason) card.appendChild(el("p", {
      className: "criterion-review-reason",
      text: "Exclusion reason: " + criterion.review.exclusion_reason,
    }));

    if (criterion.notes) {
      card.appendChild(el("p", { text: criterion.notes }));
    }

    if (criterion.review.change_reason) card.appendChild(el("small", {
      className: "criterion-review-reason",
      text: "Last change: " + criterion.review.change_reason,
    }));

    return card;
  }

  function blockingQuestions() {
    var questions = {};
    ctx.draft.criteria.forEach(function (criterion) {
      if (!criterionIncluded(criterion) || criterionConfirmed(criterion)) return;
      criterion.missing_fields.forEach(function (field) {
        var parts = missingParts(field);
        if (!parts) return;
        var id = field;
        if (!questions[id]) questions[id] = { id: id, field: field, parts: parts, criteria: [] };
        questions[id].criteria.push(criterion);
      });
    });
    return Object.keys(questions).map(function (key) { return questions[key]; }).sort(function (left, right) {
      var leftRank = left.parts && left.parts.kind === "field" ? 0 : 1;
      var rightRank = right.parts && right.parts.kind === "field" ? 0 : 1;
      return leftRank - rightRank;
    });
  }

  function openTenderQuestion(question) {
    var row = question.parts ? byId("ledger-field-" + question.parts.key) : null;
    if (!row) return showMessage("The tender finding is unavailable for correction.", "error");
    window.history.replaceState({}, "", pageUrl("ledger"));
    applyPageMode("ledger");
    var correctionButton = row.querySelector(".ledger-review > button");
    var workspaceRow = row.nextElementSibling;
    row.hidden = false;
    if (correctionButton && workspaceRow && workspaceRow.hidden) correctionButton.click();
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    showMessage("Check " + friendlyKey(question.parts.key) + " against the tender, then save only if a correction is required.", "ok");
  }

  function renderBlockingQuestions() {
    var host = byId("contract-questions");
    var questions = blockingQuestions();
    var sourceGapCount = (ctx.draft.unautomated_requirements || []).length;
    var attentionCount = ctx.draft.criteria.filter(criterionNeedsAttention).length;
    var needsReview = questions.length || sourceGapCount || attentionCount;
    host.textContent = "";
    host.appendChild(el("h3", { text: needsReview ? "Checklist needs review" : "Checklist is ready for approval" }));
    host.appendChild(el("p", {
      text: questions.length
        ? questions.length + " included check" + (questions.length === 1 ? " depends" : "s depend") + " on unresolved tender information. Correct the tender finding or explicitly exclude the affected check."
        : sourceGapCount
          ? sourceGapCount + " tender source passage" + (sourceGapCount === 1 ? " could" : "s could") + " not be classified reliably. Open All checks → Needs attention to review the exact document pages before approval."
          : attentionCount
            ? attentionCount + " included check" + (attentionCount === 1 ? " needs" : "s need") + " review. Open All checks → Needs attention and resolve or confirm each check before approval."
          : "No included checks have unresolved tender inputs. Save any edits, then approve the checklist to upload bids.",
    }));
    var list = el("div", { className: "contract-question-list" });
    questions.forEach(function (question) {
      var row = el("div", { className: "contract-question" + (ctx.blockersRevealed ? " blocker" : "") });
      row.appendChild(el("strong", { text: friendlyMissing(question.field) }));
      row.appendChild(el("small", {
        text: question.criteria.length
          ? "Affects: " + question.criteria.map(function (item) { return item.title; }).join(", ")
          : "Tender finding needs confirmation before a complete evaluation.",
      }));
      var actions = el("div", { className: "question-actions" });
      if (question.parts && question.parts.kind === "field" && ctx.ledgerEntriesByKey[question.parts.key]) {
        var entry = ctx.ledgerEntriesByKey[question.parts.key];
        row.setAttribute("role", "button");
        row.setAttribute("tabindex", "0");
        row.title = "Open " + (entry.label || friendlyKey(question.parts.key)) + " for review";
        row.addEventListener("click", function () { openTenderQuestion(question); });
        row.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openTenderQuestion(question);
          }
        });
        var review = el("button", {
          type: "button",
          text: entry.status === "found" ? "Review and confirm" : "Review finding",
        });
        review.addEventListener("click", function (event) {
          event.stopPropagation();
          openTenderQuestion(question);
        });
        actions.appendChild(review);
      } else {
        actions.appendChild(el("small", {
          text: "No tender finding can resolve this gap. Open the affected check above and explicitly exclude it if it should not be evaluated.",
        }));
      }
      row.appendChild(actions);
      list.appendChild(row);
    });
    host.appendChild(list);
  }

  return {
    renderDraft: renderDraft,
    renderChecklistCategoryNav: renderChecklistCategoryNav,
    renderCriterionRow: renderEvaluationGroupRow,
    toggleCriterionIncluded: toggleCriterionIncluded,
    setCriterionConfirmation: setCriterionConfirmation,
    renderCriterionDetail: renderCriterionDetail,
    renderChecklistSummary: renderChecklistSummary,
    renderChecklistFamilyFilter: renderChecklistFamilyFilter,
    renderChecklistOriginFilter: renderChecklistOriginFilter,
    criterionMatchesFilters: criterionMatchesFilters,
    criterionField: criterionField,
    renderCriterion: renderCriterion,
    blockingQuestions: blockingQuestions,
    openTenderQuestion: openTenderQuestion,
    renderBlockingQuestions: renderBlockingQuestions,
  };
}
