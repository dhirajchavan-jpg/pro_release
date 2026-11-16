"use strict";
/* Evaluation results compatibility facade. */

import {
  criterionKind,
  criterionLabel,
  criterionTitle as findCriterionTitle,
  displayReason,
  resultCheckKey,
  resultChecks,
  resultCounts,
  resultExplanation,
  resultKind,
  resultLabel,
  reviewPanelId,
  shortId,
} from "./results-model.js";
import { installResultsActions } from "./results-actions.js";
import { installResultsView } from "./results-view.js";

export function installResults(ctx) {
  var view = installResultsView(ctx);
  var actions = installResultsActions(ctx);
  var friendlyKey = function () { return ctx.friendlyKey.apply(ctx, arguments); };
  Object.assign(ctx, {
    loadResults: actions.loadResults,
    renderClaim: view.renderClaim,
    renderClaimPages: view.renderClaimPages,
    resultKind: resultKind,
    resultLabel: resultLabel,
    displayReason: displayReason,
    resultExplanation: resultExplanation,
    criterionKind: criterionKind,
    decisionStats: view.decisionStats,
    criterionLabel: criterionLabel,
    criterionTitle: function (criterion) { return findCriterionTitle(criterion, friendlyKey); },
    reviewPanelId: reviewPanelId,
    renderHumanReview: actions.renderHumanReview,
    resultChecks: resultChecks,
    resultCheckKey: resultCheckKey,
    resultCounts: resultCounts,
    renderResultEvidence: view.renderResultEvidence,
    renderResultWorkspace: view.renderResultWorkspace,
    renderAwardSuggestion: view.renderAwardSuggestion,
    renderResults: view.renderResults,
    shortId: shortId,
    loadReviewLog: actions.loadReviewLog,
    renderReviewLog: view.renderReviewLog,
  });
}
